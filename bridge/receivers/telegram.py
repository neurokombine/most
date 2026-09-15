"""Приёмник Telegram: длинный опрос getUpdates с персистентным смещением.

Списано с боевого `approve_bot.py`: смещение живёт в базе (иначе после
перезапуска бот заново отвечает на вчерашнее), три класса ошибок разведены
по поведению, токен маскируется в любом тексте до печати.
"""
from __future__ import annotations

from pathlib import Path

import requests

from ..narrator import TELEGRAM_LIMIT, chunk
from .base import (Attachment, BridgeConflict, FileTooBig, Incoming, RateLimited,
                   Receiver, TokenRejected, mask)

API = "https://api.telegram.org/bot{token}/{method}"
FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
LONG_POLL_TIMEOUT = 25
OFFSET_KEY = "telegram_offset"
ALLOWED_UPDATES = ["message"]

# Пределы Bot API, не наши: скачать бот может файл до 20 МБ, отправить — до 50.
# Больше — не «попробуйте снова», а другой путь; так и говорим человеку.
DOWNLOAD_LIMIT = 20 * 1024 * 1024
UPLOAD_LIMIT = 50 * 1024 * 1024
FILE_TIMEOUT = 180

# Поля сообщения с файлом → наш общий вид. Порядок важен: у сообщения бывает
# и документ, и подпись, но два файла в одном сообщении Telegram не пришлёт.
# Голосовое стоит первым: с этапа 4 мост его слышит, и разбирать его надо
# раньше остальных полей.
FILE_FIELDS = (("voice", "voice"), ("video_note", "video_note"), ("document", "file"),
               ("photo", "photo"), ("video", "video"), ("audio", "audio"))
TOO_BIG_MARKS = ("file is too big", "file_id_invalid_too_big")
# Что Telegram покажет голосовым кружком, а что — обычным аудио-файлом.
VOICE_SUFFIXES = (".ogg", ".oga", ".opus")


class TelegramReceiver(Receiver):
    channel = "telegram"
    limit = TELEGRAM_LIMIT
    download_limit = DOWNLOAD_LIMIT
    upload_limit = UPLOAD_LIMIT

    def __init__(self, token: str, store, session=None, long_poll_timeout: int = LONG_POLL_TIMEOUT):
        self.token = token
        self.store = store
        self.session = session or requests.Session()
        self.long_poll_timeout = long_poll_timeout

    # --- служебное ----------------------------------------------------------

    def _url(self, method: str) -> str:
        return API.format(token=self.token, method=method)

    def _mask(self, text) -> str:
        return mask(text, [self.token])

    def _offset(self) -> int:
        try:
            return int(self.store.get_setting(OFFSET_KEY, "0") or 0)
        except (TypeError, ValueError):
            return 0

    # --- опрос --------------------------------------------------------------

    def poll_once(self) -> list[Incoming]:
        params = {"offset": self._offset(), "timeout": self.long_poll_timeout,
                  "allowed_updates": ",".join(ALLOWED_UPDATES)}
        resp = self.session.get(self._url("getUpdates"), params=params,
                                timeout=self.long_poll_timeout + 15)

        if resp.status_code == 409:
            raise BridgeConflict("на бота уже кто-то подписан (вебхук или второй мост)")
        if resp.status_code in (401, 403, 404):
            # 404 — адреса такого бота не существует: токен пустой или битый.
            raise TokenRejected(f"Telegram не признал токен ({resp.status_code})")
        if resp.status_code == 429:
            raise RateLimited("Telegram просит подождать", retry_after=_retry_after(resp))

        try:
            data = resp.json() or {}
        except ValueError:
            # Не-JSON (заглушка прокси, 5xx) — пропускаем заход, смещение не двигаем:
            # на следующем опросе Telegram пришлёт тот же диапазон.
            self.store.note("error", channel=self.channel,
                            text="ответ getUpdates не разобрался")
            return []

        if not data.get("ok", True):
            description = self._mask(data.get("description") or "")
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry:
                raise RateLimited(f"Telegram: {description}", retry_after=int(retry))
            raise RateLimited(f"Telegram: {description}", retry_after=None)

        incoming: list[Incoming] = []
        for update in data.get("result") or []:
            try:
                parsed = self._to_incoming(update)
            except Exception as exc:                         # noqa: BLE001
                self.store.note("error", channel=self.channel,
                                text=self._mask(f"апдейт не разобран: {exc}"))
                parsed = None
            if parsed is not None:
                incoming.append(parsed)
            try:
                self.store.set_setting(OFFSET_KEY, int(update["update_id"]) + 1)
            except (KeyError, TypeError, ValueError):
                self.store.note("error", channel=self.channel,
                                text="апдейт без update_id — смещение не сдвинуто")
        return incoming

    def _to_incoming(self, update: dict) -> Incoming | None:
        message = update.get("message") or {}
        attachments = _attachments(message)
        # Подпись к файлу — это задание про него: «посчитай итог по этой таблице».
        text = (message.get("text") or message.get("caption") or "").strip()
        if not text and not attachments:
            return None                                       # сказать нечего и файла нет
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        return Incoming(
            channel=self.channel,
            chat_id=int(chat.get("id")),
            user_id=int(sender.get("id")),
            text=text,
            thread_id=int(message.get("message_thread_id") or 0),
            name=_who(sender),
            raw=update,
            attachments=attachments,
        )

    # --- ответ --------------------------------------------------------------

    def send(self, chat_id: int, text: str) -> None:
        for part in chunk(text, self.limit):
            self.session.post(self._url("sendMessage"),
                              data={"chat_id": chat_id, "text": part,
                                    "disable_web_page_preview": True},
                              timeout=30)

    # --- файлы --------------------------------------------------------------

    def fetch(self, attachment: Attachment) -> bytes:
        """Скачивает присланный файл: getFile → ссылка → тело.

        Размер проверяем до запроса, если Telegram его назвал: незачем ходить
        за файлом, который всё равно не дадут.
        """
        if attachment.size and attachment.size > self.download_limit:
            raise FileTooBig("Telegram не отдаёт файлы больше 20 МБ",
                             size=attachment.size, limit=self.download_limit)

        resp = self.session.get(self._url("getFile"),
                                params={"file_id": attachment.file_id}, timeout=60)
        try:
            data = resp.json() or {}
        except ValueError:
            data = {}
        if not data.get("ok"):
            description = self._mask(data.get("description") or f"код {resp.status_code}")
            if any(mark in description.lower() for mark in TOO_BIG_MARKS):
                raise FileTooBig("Telegram не отдаёт файлы больше 20 МБ",
                                 size=attachment.size, limit=self.download_limit)
            raise RuntimeError(f"не смог забрать файл: {description}")

        result = data.get("result") or {}
        size = int(result.get("file_size") or attachment.size or 0)
        if size > self.download_limit:
            raise FileTooBig("Telegram не отдаёт файлы больше 20 МБ",
                             size=size, limit=self.download_limit)

        body = self.session.get(
            FILE_API.format(token=self.token, path=result.get("file_path") or ""),
            timeout=FILE_TIMEOUT)
        if getattr(body, "status_code", 200) >= 400:
            raise RuntimeError(f"не смог забрать файл: код {body.status_code}")
        return body.content

    def send_voice(self, chat_id: int, path, caption: str = "") -> None:
        """Отвечает голосом. ogg/opus Telegram покажет кружком, прочее — аудио-файлом.

        Перегонять wav в ogg — дело ffmpeg, и он есть не везде. Нет его —
        уходит `sendAudio`: слышно то же самое, вид сообщения другой.
        """
        path = Path(path)
        size = path.stat().st_size
        if size > self.upload_limit:
            raise FileTooBig("Telegram не пропускает файлы больше 50 МБ",
                             size=size, limit=self.upload_limit)

        voice_like = path.suffix.lower() in VOICE_SUFFIXES
        method, field = ("sendVoice", "voice") if voice_like else ("sendAudio", "audio")
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        with open(path, "rb") as body:
            resp = self.session.post(self._url(method), data=data,
                                     files={field: (path.name, body)},
                                     timeout=FILE_TIMEOUT)
        try:
            answer = resp.json() or {}
        except ValueError:
            answer = {}
        if not answer.get("ok", resp.status_code < 400):
            raise RuntimeError("не смог отправить голосом: "
                               + self._mask(answer.get("description")
                                            or f"код {resp.status_code}"))

    def send_file(self, chat_id: int, path, caption: str = "") -> None:
        """Отправляет файл документом — и снимок тоже: иначе Telegram его пережмёт."""
        path = Path(path)
        size = path.stat().st_size
        if size > self.upload_limit:
            raise FileTooBig("Telegram не пропускает файлы больше 50 МБ",
                             size=size, limit=self.upload_limit)

        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        with open(path, "rb") as body:
            resp = self.session.post(self._url("sendDocument"), data=data,
                                     files={"document": (path.name, body)},
                                     timeout=FILE_TIMEOUT)
        try:
            answer = resp.json() or {}
        except ValueError:
            answer = {}
        if not answer.get("ok", resp.status_code < 400):
            raise RuntimeError("не смог отправить файл: "
                               + self._mask(answer.get("description")
                                            or f"код {resp.status_code}"))


def _attachments(message: dict) -> list[Attachment]:
    """Файл из сообщения в общем виде. Двух файлов в одном сообщении не бывает."""
    for field, kind in FILE_FIELDS:
        body = message.get(field)
        if not body:
            continue
        if field == "photo":
            # Снимок приходит лесенкой размеров; берём самый крупный.
            body = sorted(body, key=lambda item: item.get("file_size") or 0)[-1]
        return [Attachment(kind=kind, file_id=str(body.get("file_id") or ""),
                           file_name=str(body.get("file_name") or ""),
                           size=int(body.get("file_size") or 0),
                           duration=int(body.get("duration") or 0), raw=body)]
    return []

def _who(sender: dict) -> str:
    """Имя отправителя: имя с фамилией, а нет их — «собачка» с прозвищем."""
    parts = [str(sender.get("first_name") or "").strip(),
             str(sender.get("last_name") or "").strip()]
    name = " ".join(p for p in parts if p)
    if name:
        return name
    nick = str(sender.get("username") or "").strip()
    return f"@{nick}" if nick else ""


def _retry_after(resp) -> int | None:
    value = (resp.headers or {}).get("Retry-After")
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(((resp.json() or {}).get("parameters") or {}).get("retry_after"))
    except Exception:                                          # noqa: BLE001
        return None
