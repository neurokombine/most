"""Маршрутизатор: кому отвечаем, в какой папке работаем, что говорим.

Порядок один и тот же для обоих мессенджеров:
  1. свой или чужой — чужому молчим, но пишем в журнал;
  2. команда словами («покажи проекты», «стоп», «кто стучался»);
  3. иначе — задача нейросети в папке связки, ответ в тот же канал.

Ключ связки — тройка (канал, чат, тема). В личке и в Max тема всегда 0;
темы форума Telegram лягут в тот же ключ, когда до них дойдут руки.

С этапа 2 задача не выполняется прямо здесь: она уходит в очередь
(`works.WorkPool`) и работает своим потоком, а ответ в чат отправляет главный
цикл, когда работа кончится. Иначе мост глохнет на все пятнадцать минут,
пока нейросеть думает, — и не слышит даже слова «стоп».
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import changes, narrator, texts
from .executor import Executor
from .receivers.base import Incoming
from .works import WorkPool

MOSCOW = timezone(timedelta(hours=3))

LIMITS = {"telegram": narrator.TELEGRAM_LIMIT, "max": narrator.MAX_LIMIT}

SWITCH_RE = re.compile(r"^\s*(?:работаем|работай|переходим|перейди)\s+"
                       r"(?:с|со|в|на)\s+(?:папкой\s+|проектом\s+)?[«\"']?(?P<name>[^«»\"']+?)[»\"']?\s*$",
                       re.IGNORECASE)
KNOCKS_RE = re.compile(r"кто\s+(?:ко\s+мне\s+)?стуч", re.IGNORECASE)
STOP_RE = re.compile(r"^\s*(?:стоп|хватит|прерви(?:сь)?|останови(?:сь)?|отмени|"
                     r"остановись,?\s+пожалуйста)\s*[.!]?\s*$", re.IGNORECASE)
NEW_SESSION_RE = re.compile(r"нов(?:ый|ая)\s+(?:разговор|сесси|беседа)|"
                            r"начн(?:и|ём|ем)\s+(?:заново|с\s+чистого)|"
                            r"забудь\s+(?:всё|все|прошл)", re.IGNORECASE)
CHANGES_RE = re.compile(r"(?:что\s+(?:там\s+)?получилось|что\s+изменилось|"
                        r"что\s+появилось|покажи\s+результат)", re.IGNORECASE)
HISTORY_RE = re.compile(r"что\s+ты\s+(?:делал|сделал)|"
                        r"(?:покажи|последние)\s+(?:мои\s+)?(?:работ|задач)|"
                        r"чем\s+ты\s+занимал", re.IGNORECASE)
PROJECTS_RE = re.compile(r"(?:покажи|какие|список)\s+(?:мои\s+)?(?:проект|папк)", re.IGNORECASE)
HELP_RE = re.compile(r"^\s*(?:/?help|/start|помощь|что\s+ты\s+умеешь)\s*$", re.IGNORECASE)


@dataclass
class Reply:
    channel: str
    chat_id: int
    text: str


def to_moscow(raw: str) -> str:
    """Время в журнале хранится в UTC; человеку показываем московское."""
    try:
        return datetime.fromisoformat(raw).astimezone(MOSCOW).strftime("%d.%m %H:%M МСК")
    except (TypeError, ValueError):
        return str(raw)


def moscow_from_epoch(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, MOSCOW).strftime("%d.%m %H:%M МСК")


def epoch_of(raw: str) -> float:
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        return 0.0


def how_long(seconds) -> str:
    """Сколько длилась работа — словами, а не в секундах с точкой."""
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "сколько шла — не знаю"
    if seconds < 60:
        return f"{seconds} с"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} мин {rest} с" if rest else f"{minutes} мин"


class Router:
    def __init__(self, config, store, executor: Executor, pool: WorkPool | None = None):
        self.config = config
        self.store = store
        self.executor = executor
        self.pool = pool or WorkPool(executor=executor, store=store,
                                     max_parallel=getattr(config, "parallel", 1))

    # --- папки проектов -----------------------------------------------------

    def projects(self) -> list[str]:
        root = Path(self.config.projects_dir)
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir()
                      if p.is_dir() and not p.name.startswith("."))

    def default_project(self) -> str | None:
        found = self.projects()
        return found[0] if found else None

    # --- главный вход -------------------------------------------------------

    def handle(self, incoming: Incoming) -> list[str]:
        """Возвращает готовые сообщения в тот же канал. Пустой список = молчим."""
        text = (incoming.text or "").strip()

        if not self.store.is_allowed(incoming.channel, incoming.user_id):
            # Молчим в чат, но пишем в журнал: без записи молчание неотличимо от поломки.
            self.store.note_stranger(incoming.channel, incoming.chat_id,
                                     incoming.user_id, text)
            return []

        if not text:
            return []

        limit = LIMITS.get(incoming.channel, narrator.TELEGRAM_LIMIT)

        if HELP_RE.match(text):
            return narrator.chunk(texts.HELP, limit)
        if STOP_RE.match(text):
            return narrator.chunk(self._stop(incoming), limit)
        if NEW_SESSION_RE.search(text):
            return narrator.chunk(self._new_session(incoming), limit)
        if CHANGES_RE.search(text):
            return narrator.chunk(self._changes(incoming), limit)
        if HISTORY_RE.search(text):
            return narrator.chunk(self._history(incoming), limit)
        if KNOCKS_RE.search(text):
            return narrator.chunk(self._knocks(), limit)
        if PROJECTS_RE.search(text):
            return narrator.chunk(self._projects_list(incoming), limit)
        switch = SWITCH_RE.match(text)
        if switch:
            return narrator.chunk(self._switch(incoming, switch.group("name").strip()), limit)

        return self._work(incoming, text, limit)

    # --- команды ------------------------------------------------------------

    def _knocks(self) -> str:
        knocks = self.store.recent_strangers(limit=20)
        if not knocks:
            return texts.NOBODY_KNOCKED
        lines = [texts.KNOCKS_HEADER]
        for row in knocks:
            lines.append(texts.KNOCK_LINE.format(
                at=to_moscow(row["at"]), channel=row["channel"],
                user_id=row["user_id"], text=row["text"] or ""))
        lines.append("")
        lines.append(texts.KNOCKS_FOOTER)
        return "\n".join(lines)

    def _current_project(self, incoming: Incoming) -> str | None:
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        return link["project"] if link else None

    def _projects_list(self, incoming: Incoming) -> str:
        found = self.projects()
        if not found:
            return texts.PROJECTS_EMPTY
        current = self._current_project(incoming) or self.default_project()
        lines = [texts.PROJECTS_HEADER]
        for name in found:
            lines.append(texts.PROJECT_LINE.format(
                mark="сейчас здесь → " if name == current else "", name=name))
        return "\n".join(lines)

    def _switch(self, incoming: Incoming, name: str) -> str:
        found = self.projects()
        if not found:
            return texts.PROJECTS_EMPTY
        match = next((p for p in found if p.lower() == name.lower()), None)
        if match is None:
            known = "\n".join(texts.PROJECT_LINE.format(mark="", name=p) for p in found)
            return texts.PROJECT_UNKNOWN.format(name=name, known=known)
        self.store.upsert_link(incoming.channel, incoming.chat_id, incoming.thread_id,
                               project=match)
        self.store.set_link_project(incoming.channel, incoming.chat_id,
                                    incoming.thread_id, match)
        return texts.PROJECT_SWITCHED.format(name=match)

    # --- этап 2: остановка, новый разговор, что получилось, что делала ------

    def key_of(self, incoming: Incoming):
        return (incoming.channel, int(incoming.chat_id), int(incoming.thread_id))

    def _stop(self, incoming: Incoming) -> str:
        """«Стоп»: гасим работу. Ответ «остановила» придёт от самой работы."""
        work = self.pool.stop(self.key_of(incoming))
        if work is None:
            return texts.STOP_NOTHING_TO_STOP
        return ""          # молчим: через секунду придёт «остановила» и что успела

    def _new_session(self, incoming: Incoming) -> str:
        link = self._link_with_project(incoming)
        if link is None:
            return texts.PROJECT_NONE
        if self.pool.busy(self.key_of(incoming)):
            return texts.ALREADY_WORKING
        self.store.reset_session(link["id"], str(uuid.uuid4()))
        return texts.SESSION_RESET.format(project=link["project"])

    def _changes(self, incoming: Incoming) -> str:
        """Показывает папку, а не отчёт: это и есть проверка «отчёт ≠ результат»."""
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        job = self.store.last_finished_job(link["id"]) if link else None
        if link is None or job is None or not link["project"]:
            return texts.CHANGES_NO_WORK

        since = self._job_start_epoch(job)
        workdir = Path(self.config.projects_dir) / link["project"]
        found = changes.changed_files(workdir, since=since, limit=changes.LIMIT)
        when = moscow_from_epoch(since) if since else to_moscow(job["started_at"])
        if not found:
            return texts.CHANGES_EMPTY.format(when=when, project=link["project"])

        lines = [texts.CHANGES_HEADER.format(when=when, project=link["project"])]
        lines += [texts.CHANGES_LINE.format(name=name, at=moscow_from_epoch(mtime))
                  for name, mtime in found]
        if len(found) >= changes.LIMIT:
            lines.append(texts.CHANGES_MORE.format(limit=changes.LIMIT))
        lines.append("")
        lines.append(texts.CHANGES_FOOTER)
        return "\n".join(lines)

    @staticmethod
    def _job_start_epoch(job) -> float:
        """Метка отсчёта — НАЧАЛО последней работы.

        В meta.json смотрим поле started_at, а не время правки самого файла:
        файл переписывается на финише работы, и всё, что она успела сделать,
        оказалось бы «старее» его — список менялся бы пустым.
        """
        job_dir = job["dir"] or ""
        if job_dir:
            meta = Path(job_dir) / "meta.json"
            try:
                started = json.loads(meta.read_text(encoding="utf-8")).get("started_at")
                if started:
                    return epoch_of(started)
            except (OSError, ValueError, AttributeError):
                pass
        return epoch_of(job["started_at"])

    def _history(self, incoming: Incoming) -> str:
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        rows = self.store.jobs_of_link(link["id"], limit=5) if link else []
        if not rows:
            return texts.JOBS_EMPTY
        lines = [texts.JOBS_HEADER]
        for row in rows:
            lines.append(texts.JOB_LINE.format(
                at=to_moscow(row["started_at"]),
                how_long=how_long(row["duration_sec"]),
                outcome=texts.JOB_OUTCOME.get(row["state"], row["state"]),
                prompt=(row["prompt_head"] or "")[:60]))
        return "\n".join(lines)

    # --- работа -------------------------------------------------------------

    def _link_with_project(self, incoming: Incoming):
        """Связка с папкой: заводим при первом сообщении, папка — текущая или первая."""
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        if link is not None and link["project"]:
            return link
        project = self.default_project()
        if project is None:
            return None
        self.store.upsert_link(incoming.channel, incoming.chat_id, incoming.thread_id,
                               project=project)
        self.store.set_link_project(incoming.channel, incoming.chat_id,
                                    incoming.thread_id, project)
        return self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)

    def _work(self, incoming: Incoming, text: str, limit: int) -> list[str]:
        """Ставит задачу в очередь. Ответ с результатом придёт отдельным сообщением."""
        link = self._link_with_project(incoming)
        if link is None:
            return narrator.chunk(texts.PROJECT_NONE, limit)

        key = self.key_of(incoming)
        if self.pool.busy(key):
            return narrator.chunk(texts.ALREADY_WORKING, limit)
        if not self.pool.has_free_slot():
            return narrator.chunk(
                texts.ALL_HANDS_BUSY.format(parallel=self.pool.max_parallel), limit)

        workdir = Path(self.config.projects_dir) / link["project"]
        work = self.pool.submit(link=link, channel=incoming.channel,
                                chat_id=incoming.chat_id, thread_id=incoming.thread_id,
                                prompt=text, workdir=workdir,
                                resume=bool(link["session_started"]))
        if work is None:                      # кто-то успел раньше на доли секунды
            return narrator.chunk(texts.ALREADY_WORKING, limit)
        return narrator.chunk(texts.WORK_ACCEPTED, limit)

    # --- ответ, когда работа кончилась --------------------------------------

    def finished_messages(self, work) -> list[str]:
        """Что сказать в чат про доделанную работу. Зовёт главный цикл."""
        limit = LIMITS.get(work.channel, narrator.TELEGRAM_LIMIT)
        result = work.result
        parts: list[str] = []
        if work.session_restarted:
            parts.append(texts.SESSION_LOST_NEW)

        if result is None:
            parts.append(texts.WORK_EMPTY_ANSWER)
        elif result.stopped:
            parts.append(texts.STOPPED_BY_HAND)
            parts.append(self._managed(result))
        elif result.timed_out:
            budget = how_long(getattr(self.executor, "timeout", 900))
            parts.append(texts.WORK_TIMED_OUT.format(budget=budget))
            parts.append(self._managed(result))
        elif not result.ok:
            self.store.note("error", channel=work.channel, chat_id=work.chat_id,
                            text=(result.error or "")[:200])
            parts.append(texts.WORK_FAILED.format(
                error=result.error or "работа завершилась неудачно"))
        else:
            parts.append(result.text or narrator.final_text(result.events)
                         or texts.WORK_EMPTY_ANSWER)

        return narrator.chunk("\n\n".join(p for p in parts if p), limit) \
            or [texts.WORK_EMPTY_ANSWER]

    @staticmethod
    def _managed(result) -> str:
        partial = (result.partial or "").strip()
        return texts.WHAT_MANAGED.format(partial=partial) if partial else texts.NOTHING_MANAGED
