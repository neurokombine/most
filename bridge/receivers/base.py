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
class Attachment:
    """Файл, присланный человеком, — в одном виде для обоих мессенджеров.

    У Telegram есть только `file_id`, по нему файл добывается в два захода
    (`getFile` → скачать). Max отдаёт прямую ссылку сразу. Общий знаменатель:
    приёмник умеет `fetch(attachment) -> bytes`, и кто как это делает, знает
    только он сам.
    """

    kind: str             # "file" | "photo" | "video" | "audio" | "voice"
    file_id: str = ""     # Telegram: file_id; Max: token вложения
    file_name: str = ""   # у снимков с телефона имени не бывает вовсе
    size: int = 0         # 0 — значит, мессенджер размера не сказал
    url: str = ""         # Max отдаёт ссылку сразу, Telegram — нет
    # Длина записи в секундах, если мессенджер её назвал. Нужна голосу: слишком
    # длинное голосовое отвергается до скачивания, а не после минут расшифровки.
    duration: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class Incoming:
    """Сообщение из любого мессенджера в одном виде."""

    channel: str          # "telegram" | "max"
    chat_id: int
    user_id: int
    text: str
    thread_id: int = 0    # тема форума; в личке и в Max всегда 0
    raw: dict = field(default_factory=dict)
    # Подпись к файлу приходит в `text`: она и есть задание про этот файл.
    attachments: list = field(default_factory=list)


class BridgeConflict(RuntimeError):
    """Обновления отдают не нам: вебхук или второй запущенный мост."""


class TokenRejected(RuntimeError):
    """Мессенджер не признал токен: перевыпущен, отозван или бота удалили."""


class FileTooBig(RuntimeError):
    """Файл больше, чем пускает мессенджер. Чинится не повтором, а другим путём.

    Отдельный класс, а не общая ошибка: человеку тут нужен не «попробуйте
    снова», а честное «столько через чат не проходит, вот путь на сервере».
    """

    def __init__(self, message: str, size: int = 0, limit: int = 0):
        super().__init__(message)
        self.size = size
        self.limit = limit


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
    download_limit = 20 * 1024 * 1024    # сколько мессенджер даёт скачать
    upload_limit = 50 * 1024 * 1024      # сколько мессенджер даёт отправить

    def poll_once(self) -> list[Incoming]:
        raise NotImplementedError

    def send(self, chat_id: int, text: str) -> None:
        raise NotImplementedError

    def fetch(self, attachment: Attachment) -> bytes:
        """Скачивает присланный файл. Больше предела — FileTooBig."""
        raise NotImplementedError

    def send_file(self, chat_id: int, path, caption: str = "") -> None:
        """Отправляет файл с диска. Больше предела — FileTooBig."""
        raise NotImplementedError

    def send_voice(self, chat_id: int, path, caption: str = "") -> None:
        """Отправляет запись голосом. Нет ogg — уйдёт обычным аудио."""
        raise NotImplementedError
