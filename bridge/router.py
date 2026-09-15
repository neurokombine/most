"""Маршрутизатор: кому отвечаем, в какой папке работаем, что говорим.

Порядок один и тот же для обоих мессенджеров:
  1. свой или чужой — чужому молчим, но пишем в журнал;
  2. команда словами («покажи проекты», «работаем с …», «кто стучался»);
  3. иначе — задача нейросети в папке связки, ответ в тот же канал.

Ключ связки — тройка (канал, чат, тема). В личке и в Max тема всегда 0;
темы форума Telegram лягут в тот же ключ, когда до них дойдут руки.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import narrator, texts
from .executor import Executor
from .receivers.base import Incoming

MOSCOW = timezone(timedelta(hours=3))

LIMITS = {"telegram": narrator.TELEGRAM_LIMIT, "max": narrator.MAX_LIMIT}

SWITCH_RE = re.compile(r"^\s*(?:работаем|работай|переходим|перейди)\s+"
                       r"(?:с|со|в|на)\s+(?:папкой\s+|проектом\s+)?[«\"']?(?P<name>[^«»\"']+?)[»\"']?\s*$",
                       re.IGNORECASE)
KNOCKS_RE = re.compile(r"кто\s+(?:ко\s+мне\s+)?стуч", re.IGNORECASE)
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


class Router:
    def __init__(self, config, store, executor: Executor):
        self.config = config
        self.store = store
        self.executor = executor

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

    # --- работа -------------------------------------------------------------

    def _work(self, incoming: Incoming, text: str, limit: int) -> list[str]:
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        if link is None or not link["project"]:
            project = self.default_project()
            if project is None:
                return narrator.chunk(texts.PROJECT_NONE, limit)
            link = self.store.upsert_link(incoming.channel, incoming.chat_id,
                                          incoming.thread_id, project=project)
            if not link["project"]:
                self.store.set_link_project(incoming.channel, incoming.chat_id,
                                            incoming.thread_id, project)
                link = self.store.get_link(incoming.channel, incoming.chat_id,
                                           incoming.thread_id)

        workdir = Path(self.config.projects_dir) / link["project"]
        session_id = link["session_id"]

        job_id = self.store.start_job(link_id=link["id"], channel=incoming.channel,
                                      chat_id=incoming.chat_id, session_id=session_id,
                                      prompt=text, job_dir="")
        self.store.touch_link(link["id"], state="working")

        result = self.executor.run(text, workdir, session_id=session_id)

        self.store.finish_job(job_id, state="done" if result.ok else "failed",
                              exit_code=result.exit_code)
        self.store.touch_link(link["id"], state="idle")

        if result.timed_out:
            minutes = max(1, int(getattr(self.executor, "timeout", 900) // 60))
            return narrator.chunk(texts.WORK_TIMED_OUT.format(minutes=minutes), limit)
        if not result.ok:
            self.store.note("error", channel=incoming.channel, chat_id=incoming.chat_id,
                            user_id=incoming.user_id, text=(result.error or "")[:200])
            return narrator.chunk(texts.WORK_FAILED.format(
                error=result.error or "работа завершилась неудачно"), limit)

        answer = result.text or narrator.final_text(result.events)
        return narrator.chunk(answer, limit) or [texts.WORK_EMPTY_ANSWER]
