"""Исполнитель: адаптер к нейросети в папке проекта.

Интерфейс один — `run(prompt, workdir, session_id) -> Result`. Реализаций две:
`ClaudeExecutor` (настоящий `claude -p`) и `FakeExecutor` (для тестов).

Списано с боевого `room_task.py`, четыре решения перенесены дословно:
  1. чистое окружение словарём — вложенный `claude` наследует CLAUDECODE/
     CLAUDE_CODE_ENTRYPOINT от родительской сессии и молча падает;
  2. `start_new_session=True` — работа переживает смерть родителя и перезапуск юнита;
  3. `--session-id` задаём сами, чтобы сессия была адресуема снаружи;
  4. журнал работы — папкой на диске: состояние читается с диска, не из памяти.

Ключей нейросетей здесь нет ни в каком виде: `claude` работает под сохранённым
входом по подписке. Всё, что похоже на токен, маскируется до записи в лог.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import narrator
from .receivers.base import mask

DEFAULT_TIMEOUT = 900        # бюджет времени на одну работу: 15 минут
ERR_TAIL = 800
DEFAULT_MODEL = "sonnet"     # по умолчанию не Opus: headless тянет самую дорогую модель

# Признак того, что сессия не нашлась: id протух, папку ~/.claude вычистили,
# работали на другой машине. Ловится и по тексту в потоке, и по stderr.
SESSION_LOST_MARK = "No conversation found"

# Что оставляем вложенному процессу. Всё прочее (включая CLAUDECODE*,
# CLAUDE_CODE_ENTRYPOINT и любые ключи API) отрезаем.
KEEP = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TERM", "TZ", "SHELL")
FALLBACK = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "TERM": "dumb",
}


@dataclass
class Result:
    ok: bool
    exit_code: int | None
    session_id: str
    job_id: str
    job_dir: Path
    events: list[dict] = field(default_factory=list)
    text: str = ""
    error: str = ""
    timed_out: bool = False
    stopped: bool = False          # остановлена человеком словом «стоп»
    session_lost: bool = False     # прошлый разговор не нашёлся
    resumed: bool = False          # шла продолжением прошлого разговора
    partial: str = ""              # что успела сказать до остановки
    duration_sec: float = 0.0


class RunHandle:
    """Ручка живой работы: за неё её останавливают.

    Одна ручка — одна работа. `cancel()` можно звать из другого потока: он
    гасит всю группу процессов (claude поднимает детей, одинокий kill их бросит).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._event = threading.Event()
        self.proc = None
        self.cancelled = False

    def attach(self, proc) -> None:
        with self._lock:
            self.proc = proc
            if self.cancelled:
                _kill_group(proc)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            proc = self.proc
        self._event.set()
        if proc is not None:
            _kill_group(proc)

    def wait(self, seconds: float) -> bool:
        """Ждёт, но просыпается на «стоп». True — значит, остановили."""
        return self._event.wait(seconds)


def clean_env(source: dict | None = None) -> dict:
    """Чистое окружение для вложенной нейросети."""
    source = dict(source if source is not None else os.environ)
    env = {key: source[key] for key in KEEP if source.get(key)}
    for key, value in FALLBACK.items():
        env.setdefault(key, value)
    env.setdefault("HOME", str(Path.home()))
    user = env.get("USER") or env.get("LOGNAME") or "most"
    env.setdefault("USER", user)
    env.setdefault("LOGNAME", user)
    return env


def resolve_claude_bin() -> str:
    local = Path.home() / ".local" / "bin" / "claude"
    if local.exists():
        return str(local)
    return shutil.which("claude") or "claude"


class Executor:
    """Общий интерфейс исполнителя: одна работа — один вызов."""

    def new_handle(self) -> RunHandle:
        return RunHandle()

    def run(self, prompt: str, workdir: Path, session_id: str | None = None,
            resume: bool = False, handle: RunHandle | None = None) -> Result:
        raise NotImplementedError


class FakeExecutor(Executor):
    """Подставной исполнитель: ничего не запускает, всё записывает.

    Умеет то же, что настоящий: тянуть время (`delay`), останавливаться по
    ручке, упираться в бюджет времени и врать, что сессия потерялась.
    """

    def __init__(self, text: str = "Готово.", ok: bool = True, exit_code: int = 0,
                 delay: float = 0.0, partial: str = "", lose_session: bool = False,
                 timeout: float = DEFAULT_TIMEOUT):
        self.text = text
        self.ok = ok
        self.exit_code = exit_code
        self.delay = delay
        self.partial = partial
        self.lose_session = lose_session
        self.timeout = timeout
        self.calls: list[dict] = []

    def run(self, prompt: str, workdir: Path, session_id: str | None = None,
            resume: bool = False, handle: RunHandle | None = None) -> Result:
        self.calls.append({"prompt": prompt, "workdir": Path(workdir),
                           "session_id": session_id, "resume": resume})
        session_id = session_id or str(uuid.uuid4())
        job_id = "fake-" + uuid.uuid4().hex[:8]
        started = time.monotonic()

        if resume and self.lose_session:
            return Result(ok=False, exit_code=1, session_id=session_id, job_id=job_id,
                          job_dir=Path(workdir), events=[], text="",
                          error=SESSION_LOST_MARK, session_lost=True, resumed=True,
                          duration_sec=time.monotonic() - started)

        timed_out = False
        if self.delay:
            waited = min(self.delay, self.timeout)
            stopped = handle.wait(waited) if handle is not None else bool(time.sleep(waited))
            if handle is not None and handle.cancelled:
                return Result(ok=False, exit_code=None, session_id=session_id, job_id=job_id,
                              job_dir=Path(workdir), events=[], text="", stopped=True,
                              partial=self.partial, resumed=resume,
                              duration_sec=time.monotonic() - started)
            timed_out = self.delay > self.timeout
            if timed_out:
                return Result(ok=False, exit_code=None, session_id=session_id, job_id=job_id,
                              job_dir=Path(workdir), events=[], text="", timed_out=True,
                              partial=self.partial, resumed=resume,
                              duration_sec=time.monotonic() - started)
            del stopped

        events = [{"type": "result", "subtype": "success", "is_error": not self.ok,
                   "result": self.text, "session_id": session_id}]
        return Result(ok=self.ok, exit_code=self.exit_code, session_id=session_id,
                      job_id=job_id, job_dir=Path(workdir),
                      events=events, text=self.text, resumed=resume,
                      error="" if self.ok else self.text,
                      duration_sec=time.monotonic() - started)


class ClaudeExecutor(Executor):
    """Настоящий запуск: `claude -p … --output-format stream-json --verbose`."""

    def __init__(self, jobs_dir: Path, claude_bin=None, timeout: int = DEFAULT_TIMEOUT,
                 secrets=None, skip_permissions: bool = True,
                 model: str | None = DEFAULT_MODEL, extra_args=None):
        self.jobs_dir = Path(jobs_dir)
        self.claude_bin = claude_bin or resolve_claude_bin()
        self.timeout = timeout
        self.secrets = list(secrets or [])
        self.skip_permissions = skip_permissions
        self.model = (model or "").strip() or None
        self.extra_args = [str(a) for a in (extra_args or [])]

    # --- служебное ----------------------------------------------------------

    def _bin(self) -> list[str]:
        return list(self.claude_bin) if isinstance(self.claude_bin, (list, tuple)) \
            else [str(self.claude_bin)]

    def _mask(self, text) -> str:
        return mask(text, self.secrets)

    def _new_job_dir(self) -> tuple[str, Path]:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_id, job_dir

    # --- запуск -------------------------------------------------------------

    def run(self, prompt: str, workdir: Path, session_id: str | None = None,
            resume: bool = False, handle: RunHandle | None = None) -> Result:
        workdir = Path(workdir)
        session_id = session_id or str(uuid.uuid4())
        job_id, job_dir = self._new_job_dir()
        started = time.monotonic()

        (job_dir / "prompt.md").write_text(self._mask(prompt), encoding="utf-8")
        meta = {"id": job_id, "session_id": session_id, "workdir": str(workdir),
                "resumed": bool(resume), "model": self.model,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": None}
        _write_json(job_dir / "meta.json", meta)

        # Первый вызов заводит сессию своим id, второй её продолжает.
        # Разведка этапа 0: сессия ищется по id глобально, а не по папке.
        cmd = self._bin() + [
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
        ]
        cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        out_path = job_dir / "out.jsonl"
        err_path = job_dir / "err.log"
        timed_out = False
        stopped = False

        def unfinished(error: str, mark: str) -> Result:
            (job_dir / "exit.code").write_text(mark, encoding="utf-8")
            err_path.touch()
            (job_dir / "pid").write_text("", encoding="utf-8")
            return Result(ok=False, exit_code=None, session_id=session_id, job_id=job_id,
                          job_dir=job_dir, events=[], text="", error=error,
                          resumed=bool(resume), duration_sec=time.monotonic() - started)

        try:
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                proc = subprocess.Popen(
                    cmd, cwd=str(workdir), env=clean_env(),
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    start_new_session=True, close_fds=True)
                if handle is not None:
                    handle.attach(proc)          # с этой секунды работу можно остановить
                (job_dir / "pid").write_text(str(proc.pid), encoding="utf-8")
                meta["pid"] = proc.pid
                _write_json(job_dir / "meta.json", meta)
                try:
                    exit_code = proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_group(proc)
                    exit_code = None
                if handle is not None and handle.cancelled:
                    stopped, timed_out = True, False
        except FileNotFoundError:
            # claude не найден — это чинится руками, и сказать надо по-человечески.
            return unfinished(f"claude не найден: {self._bin()[0]}", "no-binary")
        except OSError as exc:
            return unfinished(self._mask(f"не удалось запустить claude: {exc}"), "error")

        mark = "stopped" if stopped else ("timeout" if timed_out else str(exit_code))
        (job_dir / "exit.code").write_text(mark, encoding="utf-8")

        raw = out_path.read_text(encoding="utf-8", errors="replace")
        events = narrator.parse_stream(raw)
        err_tail = self._mask(err_path.read_text(encoding="utf-8", errors="replace").strip())[-ERR_TAIL:]

        meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        meta["exit_code"] = mark
        _write_json(job_dir / "meta.json", meta)

        ok = (not timed_out) and (not stopped) and exit_code == 0
        text = narrator.final_text(events) if events else ""
        partial = narrator.partial_text(events)
        session_lost = bool(resume) and not ok and (
            SESSION_LOST_MARK in err_tail or SESSION_LOST_MARK in raw)

        error = ""
        if stopped:
            error = "работа остановлена по просьбе человека"
        elif timed_out:
            error = f"работа шла дольше {self.timeout} с и была остановлена"
        elif session_lost:
            error = "прошлый разговор не нашёлся"
        elif not ok:
            error = err_tail or f"claude завершился с кодом {exit_code}"

        return Result(ok=ok, exit_code=exit_code,
                      session_id=narrator.session_id_of(events) or session_id,
                      job_id=job_id, job_dir=job_dir, events=events,
                      text="" if (stopped or timed_out) else text,
                      error=error, timed_out=timed_out, stopped=stopped,
                      session_lost=session_lost, resumed=bool(resume),
                      partial=partial, duration_sec=time.monotonic() - started)


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _kill_group(proc) -> None:
    """Гасим всю группу процессов: claude поднимает детей, одинокий kill их оставит."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
