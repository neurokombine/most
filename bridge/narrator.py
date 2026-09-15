"""Рассказчик: поток stream-json от `claude -p` → человеческие сообщения.

Модель говорит машинными событиями; человеку в чат нужен текст, разрезанный
под лимит мессенджера. Битые строки потока молча пропускаем: формат внешний,
ронять из-за одной строки весь ответ нельзя.
"""
from __future__ import annotations

import json

from . import texts

TELEGRAM_LIMIT = 4096
MAX_LIMIT = 4000        # у Max свой предел: 4000 символов на сообщение


def parse_stream(raw: str) -> list[dict]:
    """Строки JSONL → события. Всё, что не разобралось, пропускаем."""
    events = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def assistant_texts(events: list[dict]) -> list[str]:
    """Реплики нейросети по ходу работы — без вызовов инструментов."""
    out = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        content = (event.get("message") or {}).get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    out.append(text)
    return out


def result_event(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("type") == "result":
            return event
    return None


def session_id_of(events: list[dict]) -> str | None:
    for event in events:
        sid = event.get("session_id")
        if sid:
            return str(sid)
    return None


def final_text(events: list[dict]) -> str:
    """Итог: строка result, иначе последняя реплика нейросети."""
    result = result_event(events)
    if result is not None:
        body = (result.get("result") or "").strip()
        if result.get("is_error"):
            return texts.WORK_FAILED.format(error=body or "работа завершилась с ошибкой")
        if body:
            return body
    said = assistant_texts(events)
    if said:
        return said[-1]
    return texts.WORK_EMPTY_ANSWER


def delta_text(events: list[dict]) -> str:
    """Текст, собранный из живых кусочков (`--include-partial-messages`).

    Работу оборвали на полуслове — законченной реплики в потоке нет вовсе,
    а кусочки есть. Только по ним и можно сказать, что она успела.
    """
    pieces = []
    for event in events:
        if event.get("type") != "stream_event":
            continue
        inner = event.get("event") or {}
        if inner.get("type") != "content_block_delta":
            continue
        delta = inner.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            pieces.append(delta["text"])
    return "".join(pieces).strip()


def partial_text(events: list[dict], limit: int = 1500) -> str:
    """Что нейросеть успела сказать до остановки.

    Нужно там, где итога нет вовсе, — «стоп» и упёршийся бюджет времени.
    """
    said = "\n\n".join(t for t in assistant_texts(events) if t).strip()
    live = delta_text(events)
    out = live if len(live) > len(said) else said
    if not out:
        return ""
    return out if len(out) <= limit else out[:limit].rstrip() + "…"


def chunk(text: str, limit: int) -> list[str]:
    """Режет текст под лимит: по строкам, а если строка сама длиннее — насильно.

    Ничего не теряет и ничего не добавляет: склейка кусков даёт исходный текст.
    """
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        parts.append(current)
    return [p for p in parts if p]


def to_messages(events: list[dict], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Готовые сообщения в чат: итог работы, разрезанный под лимит канала."""
    if not events:
        return [texts.WORK_EMPTY_ANSWER]
    return chunk(final_text(events), limit) or [texts.WORK_EMPTY_ANSWER]
