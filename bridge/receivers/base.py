"""Общее для обоих приёмников: единый входящий объект и три класса бед.

Три класса разведены по поведению, а не по номеру кода:
  • BridgeConflict — слушателя перехватили (вебхук или второй мост). Цикл
    останавливаем: молчаливый вечный ретрай опаснее честного падения;
  • TokenRejected — токен не признан. Ретрай бессмыслен, чинится руками;
  • RateLimited  — попросили подождать. Ждём ровно столько, сколько сказали.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Incoming:
    """Сообщение из любого мессенджера в одном виде."""

    channel: str          # "telegram" | "max"
    chat_id: int
    user_id: int
    text: str
    thread_id: int = 0    # тема форума; в личке и в Max всегда 0
    raw: dict = field(default_factory=dict)


class BridgeConflict(RuntimeError):
    """Обновления отдают не нам: вебхук или второй запущенный мост."""


class TokenRejected(RuntimeError):
    """Мессенджер не признал токен: перевыпущен, отозван или бота удалили."""


class RateLimited(RuntimeError):
    """Просят притормозить. retry_after — сколько секунд ждать."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def mask(text: str, secrets) -> str:
    """Прячет токены в тексте ошибки до того, как он попадёт в лог или в чат.

    Пустой токен не маскируем: "строка".replace("", "***") вставит маркер
    между каждым символом.
    """
    out = str(text)
    for secret in secrets or []:
        if secret:
            out = out.replace(secret, "***")
    return out


class Receiver:
    """Общая часть приёмника: имя канала, лимит сообщения, хранилище."""

    channel = "?"
    limit = 4096

    def poll_once(self) -> list[Incoming]:
        raise NotImplementedError

    def send(self, chat_id: int, text: str) -> None:
        raise NotImplementedError
