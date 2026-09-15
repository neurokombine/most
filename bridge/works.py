"""Очередь работ: одна связка — одна работа за раз, разные связки — параллельно.

Почему очередь вообще нужна. Нейросеть в папке думает минутами, а мессенджер
за это время принесёт ещё три сообщения. Без очереди в одной папке окажутся два
`claude` сразу — они будут спорить за одни и те же файлы. Поэтому правило:
**на одну связку живёт ровно одна работа**, второе сообщение получает честное
«я ещё работаю над прошлым».

Разные связки (другой чат, другой человек) идут параллельно, но не больше
`max_parallel` сразу: каждый `claude -p` — это ~300 МБ памяти (замер этапа 0),
и на ученической машине с 4 ГБ двух хватает, чтобы её положить. По умолчанию — один.

Работа живёт в потоке; остановить её можно снаружи — за ручку `RunHandle`,
которая гасит всю группу процессов. Готовые работы забирает главный цикл
(`collect()`) и он же отвечает в чат: отправлять из рабочих потоков нельзя,
приёмник у канала один на всех.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import narrator
from .executor import Executor, Result, RunHandle
from .store import (JOB_DONE, JOB_FAILED, JOB_STOPPED, JOB_TIMEOUT)

DEFAULT_PARALLEL = 1
WAIT_STEP = 0.02

Key = tuple[str, int, int]


@dataclass
class Work:
    """Одна работа: что делаем, где, для кого и чем кончилось."""

    key: Key
    job_id: str
    link_id: int
    channel: str
    chat_id: int
    prompt: str
    workdir: Path
    session_id: str
    resume: bool
    started_at: float
    handle: RunHandle
    # Монотонные часы не годятся, когда надо спросить «что в папке новее начала
    # работы»: у них своё начало отсчёта. Держим рядом и настенное время.
    started_wall: float = 0.0
    result: Result | None = None
    session_restarted: bool = False      # прошлый разговор не нашёлся, завели новый
    stop_asked: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        r = self.result
        if r is None:
            return "running"
        if r.stopped:
            return JOB_STOPPED
        if r.timed_out:
            return JOB_TIMEOUT
        return JOB_DONE if r.ok else JOB_FAILED


class WorkPool:
    def __init__(self, executor: Executor, store, max_parallel: int = DEFAULT_PARALLEL):
        self.executor = executor
        self.store = store
        self.max_parallel = max(1, int(max_parallel or 1))
        self._lock = threading.Lock()
        self._running: dict[Key, Work] = {}
        self._done: list[Work] = []

    # --- что сейчас происходит ---------------------------------------------

    def busy(self, key: Key) -> bool:
        with self._lock:
            return key in self._running

    def running(self) -> int:
        with self._lock:
            return len(self._running)

    def has_free_slot(self) -> bool:
        with self._lock:
            return len(self._running) < self.max_parallel

    def current(self, key: Key) -> Work | None:
        with self._lock:
            return self._running.get(key)

    # --- поставить работу ---------------------------------------------------

    def submit(self, link, channel: str, chat_id: int, prompt: str, workdir,
               resume: bool = False, thread_id: int = 0) -> Work | None:
        """Заводит работу. None — значит, места нет: связка занята или все руки заняты."""
        key: Key = (channel, int(chat_id), int(thread_id))
        session_id = link["session_id"] or str(uuid.uuid4())

        with self._lock:
            if key in self._running or len(self._running) >= self.max_parallel:
                return None
            job_id = self.store.start_job(link_id=link["id"], channel=channel,
                                          chat_id=chat_id, session_id=session_id,
                                          prompt=prompt, job_dir="")
            work = Work(key=key, job_id=job_id, link_id=link["id"], channel=channel,
                        chat_id=int(chat_id), prompt=prompt, workdir=Path(workdir),
                        session_id=session_id, resume=bool(resume),
                        started_at=time.monotonic(), handle=self.executor.new_handle(),
                        started_wall=time.time())
            self._running[key] = work

        self.store.touch_link(link["id"], state="working")
        threading.Thread(target=self._work, args=(work,), daemon=True,
                         name=f"most-work-{job_id}").start()
        return work

    def stop(self, key: Key) -> Work | None:
        """«Стоп»: гасим работу этой связки. None — значит, гасить нечего."""
        work = self.current(key)
        if work is None:
            return None
        work.stop_asked = True
        work.handle.cancel()
        return work

    def stop_in_dir(self, workdir, skip: Key | None = None) -> list[Work]:
        """Гасит всё, что идёт в этой папке, — своё и заведённое расписанием.

        Находка этапа 2: «стоп» гасил только работу своей связки, а работа
        будильника шла в той же папке под своим ключом и оставалась жить.
        Человек видит одну папку, а не ключи связок, — значит, и останавливать
        надо по папке.
        """
        try:
            target = Path(workdir).resolve()
        except OSError:
            target = Path(workdir)
        stopped: list[Work] = []
        with self._lock:
            running = list(self._running.items())
        for key, work in running:
            if skip is not None and key == skip:
                continue
            try:
                here = work.workdir.resolve()
            except OSError:
                here = work.workdir
            if here != target:
                continue
            work.stop_asked = True
            work.handle.cancel()
            stopped.append(work)
        return stopped

    def stop_all(self) -> None:
        for key in list(self._running):
            self.stop(key)

    # --- забрать готовое ----------------------------------------------------

    def collect(self) -> list[Work]:
        """Отдаёт доделанные работы ровно один раз — их ответы уходят в чат."""
        with self._lock:
            done, self._done = self._done, []
        return done

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Для тестов и для остановки моста: дождаться, пока работы кончатся."""
        deadline = time.monotonic() + timeout
        while self.running() and time.monotonic() < deadline:
            time.sleep(WAIT_STEP)
        return not self.running()

    # --- сама работа --------------------------------------------------------

    def _work(self, work: Work) -> None:
        try:
            result = self._run_with_session(work)
        except Exception as exc:                        # noqa: BLE001
            # Поток не имеет права умереть молча: связка останется «занятой» навсегда.
            result = Result(ok=False, exit_code=None, session_id=work.session_id,
                            job_id=work.job_id, job_dir=work.workdir, events=[],
                            text="", error=f"работа сорвалась: {exc}")
        work.result = result

        self._remember(work, result)
        with self._lock:
            self._running.pop(work.key, None)
            self._done.append(work)

    def _run_with_session(self, work: Work) -> Result:
        """Продолжаем разговор; не нашёлся — заводим новый и говорим об этом."""
        result = self.executor.run(work.prompt, work.workdir, session_id=work.session_id,
                                   resume=work.resume, handle=work.handle)
        if result.session_lost and not work.handle.cancelled:
            fresh = str(uuid.uuid4())
            self.store.reset_session(work.link_id, fresh)
            work.session_id = fresh
            work.session_restarted = True
            result = self.executor.run(work.prompt, work.workdir, session_id=fresh,
                                       resume=False, handle=work.handle)
        return result

    def _remember(self, work: Work, result: Result) -> None:
        """Записываем итог на диск: после перезагрузки память демона не поможет."""
        try:
            if getattr(result, "job_dir", None):
                self.store.set_job_dir(work.job_id, str(result.job_dir))
            head = result.text or result.partial or result.error
            # Цена работы живёт в потоке событий и нигде больше: не записали
            # сейчас — «сколько потратила за сутки» посчитать будет не из чего.
            self.store.finish_job(work.job_id, work.state, exit_code=result.exit_code,
                                  duration_sec=round(time.monotonic() - work.started_at, 1),
                                  result_head=head,
                                  cost_usd=narrator.cost_of(result.events))
            if result.events or result.ok or result.stopped or result.timed_out:
                self.store.mark_session_started(work.link_id)
            self.store.touch_link(work.link_id, state="idle")
        except Exception as exc:                        # noqa: BLE001
            # Записать итог не вышло — но работу отдать в чат всё равно надо.
            print(f"мост: не записал итог работы {work.job_id}: {exc}", flush=True)
