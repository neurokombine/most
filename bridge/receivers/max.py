"""Приёмник Max: тот же длинный опрос, но своя схема обновлений.

Отличия от Telegram, проверенные живым ботом 15.09.2026:
  • базовый адрес `https://platform-api2.max.ru`, заголовок `Authorization: <token>`;
  • вместо `update_id`/`offset` — сквозной `marker`, который отдаётся в ответе
    и передаётся обратно следующим запросом; первый запрос идёт без него;
  • `*.max.ru` выдан российским удостоверяющим центром, которого нет в certifi,
    поэтому у запросов своя связка сертификатов (certs/max_ca_bundle.pem);
  • предел текста сообщения — 4000 символов, а не 4096.

Коды ошибок по dev.max.ru/docs-api: 401 — токен не признан, 429 — просят
подождать, 405 — метод больше не отдаётся (так выглядит живая вебхук-подписка:
длинный опрос боту с подпиской недоступен), 500/503 — беда на их стороне.
"""
from __future__ import annotations

import mimetypes
import time
from pathlib import Path

import requests

from ..narrator import MAX_LIMIT, chunk
from .base import (Attachment, BridgeConflict, FileTooBig, Incoming, RateLimited,
                   Receiver, TokenRejected, mask)

BASE = "https://platform-api2.max.ru"
LONG_POLL_TIMEOUT = 25
MARKER_KEY = "max_marker"
MESSAGE_UPDATES = ("message_created", "comment_created")
SEND_PAUSE = 0.6        # два сообщения в секунду в один чат — предел Max

# Файлы. Max отдаёт ссылку на присланное сразу в самом обновлении — второго
# запроса, как `getFile` у Telegram, здесь нет.
FILE_KINDS = {"file": "file", "image": "photo", "video": "video", "audio": "audio"}
FILE_TIMEOUT = 300
# Их документация обещает для типа `file` до 4 ГБ, но гигабайты через себя мост
# не гоняет: на ученической машине это память, время и молчащий бот. Свой
# потолок держим на четверти гигабайта и говорим человеку путь на сервере.
UPLOAD_LIMIT = 250 * 1024 * 1024
DOWNLOAD_LIMIT = 50 * 1024 * 1024        # тоже наш потолок: их предела в документации нет
# «Файл ещё обрабатывается»: сообщение ушло раньше, чем их сторона дожевала
# загрузку. Документация просит подождать и повторить с растущей паузой.
NOT_READY = "attachment.not.ready"
UPLOAD_SETTLE = 1.0                      # пауза после загрузки, до первой попытки
NOT_READY_TRIES = 6
NOT_READY_STEP = 2.0
# Их файловый узел ищет в multipart именно файл, а не просто поле: части без
# `Content-Type` он не видит вовсе и отвечает «There is no file in request»
# (код `upload.error`). Вид не угадался по расширению — говорим «просто байты».
DEFAULT_MIME = "application/octet-stream"

_BUNDLE = Path(__file__).resolve().parent.parent.parent / "certs" / "max_ca_bundle.pem"
CA_BUNDLE = str(_BUNDLE) if _BUNDLE.exists() else True


class MaxReceiver(Receiver):
    channel = "max"
    limit = MAX_LIMIT
    download_limit = DOWNLOAD_LIMIT
    upload_limit = UPLOAD_LIMIT

    def __init__(self, token: str, store, session=None, long_poll_timeout: int = LONG_POLL_TIMEOUT,
                 verify=CA_BUNDLE, sleeper=time.sleep):
        self.token = token
        self.store = store
        self.session = session or requests.Session()
        self.long_poll_timeout = long_poll_timeout
        self.verify = verify
        self.sleep = sleeper

    # --- служебное ----------------------------------------------------------

    @property
    def headers(self) -> dict:
        return {"Authorization": self.token}

    def _mask(self, text) -> str:
        return mask(text, [self.token])

    def _marker(self) -> int | None:
        raw = self.store.get_setting(MARKER_KEY)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # --- опрос --------------------------------------------------------------

    def poll_once(self) -> list[Incoming]:
        params = {"limit": 100, "timeout": self.long_poll_timeout}
        marker = self._marker()
        if marker is not None:
            params["marker"] = marker

        resp = self.session.get(BASE + "/updates", params=params, headers=self.headers,
                                verify=self.verify, timeout=self.long_poll_timeout + 15)

        if resp.status_code in (401, 403):
            raise TokenRejected(f"Max не признал токен ({resp.status_code})")
        if resp.status_code == 429:
            raise RateLimited("Max просит подождать", retry_after=_retry_after(resp))
        if resp.status_code == 405:
            raise BridgeConflict("Max не отдаёт обновления длинным опросом "
                                 "(похоже, у бота живёт вебхук-подписка)")

        # Разбираем тело раньше, чем судим по коду: 5xx часто приходит HTML-заглушкой
        # прокси. Её пропускаем молча и marker не двигаем — Max отдаст тот же диапазон.
        try:
            data = resp.json() or {}
        except ValueError:
            self.store.note("error", channel=self.channel,
                            text=f"ответ /updates не разобрался (код {resp.status_code})")
            return []
        if resp.status_code >= 500:
            raise RateLimited(f"Max отвечает {resp.status_code}", retry_after=None)

        incoming: list[Incoming] = []
        for update in data.get("updates") or []:
            try:
                parsed = self._to_incoming(update)
            except Exception as exc:                          # noqa: BLE001
                self.store.note("error", channel=self.channel,
                                text=self._mask(f"обновление не разобрано: {exc}"))
                parsed = None
            if parsed is not None:
                incoming.append(parsed)

        marker = data.get("marker")
        if marker is not None:
            self.store.set_setting(MARKER_KEY, int(marker))
        return incoming

    def _to_incoming(self, update: dict) -> Incoming | None:
        if update.get("update_type") not in MESSAGE_UPDATES:
            return None                                       # вход/выход бота и прочее — мимо

        message = update.get("message") or {}
        body = message.get("body") or {}
        sender = message.get("sender") or update.get("user") or {}
        recipient = message.get("recipient") or {}

        attachments = _attachments(body.get("attachments") or update.get("attachments"))
        # Подпись к файлу — это задание про него, а не просто текст рядом.
        text = (body.get("text") or update.get("text") or "").strip()
        if not text and not attachments:
            return None                                       # сказать нечего и файла нет

        chat_id = recipient.get("chat_id") or update.get("chat_id")
        user_id = sender.get("user_id") or recipient.get("user_id")
        if chat_id is None or user_id is None:
            return None

        return Incoming(
            channel=self.channel,
            chat_id=int(chat_id),
            user_id=int(user_id),
            text=text,
            thread_id=0,                                      # тем в личке Max нет
            name=_who(sender),
            group=_is_group(recipient, chat_id),
            raw=update,
            attachments=attachments,
        )


    # --- файлы --------------------------------------------------------------

    def fetch(self, attachment: Attachment) -> bytes:
        """Скачивает присланное по ссылке из самого обновления.

        Ссылка ведёт на их файловый узел (`fu.oneme.ru` и соседи), а не на
        platform-api2, и токен ему обычно не нужен. Но если он его всё-таки
        спросит — отдаём, а не сдаёмся: документация об этом молчит.
        """
        if attachment.size and attachment.size > self.download_limit:
            raise FileTooBig("файл больше, чем мост тянет через Max",
                             size=attachment.size, limit=self.download_limit)
        if not attachment.url:
            raise RuntimeError("Max не дал ссылки на этот файл")

        resp = self.session.get(attachment.url, verify=self.verify, timeout=FILE_TIMEOUT)
        if resp.status_code in (401, 403):
            resp = self.session.get(attachment.url, headers=self.headers,
                                    verify=self.verify, timeout=FILE_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"не смог забрать файл: код {resp.status_code}")
        return resp.content

    def send_file(self, chat_id: int, path, caption: str = "") -> None:
        """Три шага Max: попросить место → залить → сослаться на токен.

        Снимок уходит типом `file`, а не `image`: картинкой его пережмут,
        а человек просил файл.
        """
        self._upload_and_send(chat_id, path, caption, kind="file")

    def send_voice(self, chat_id: int, path, caption: str = "") -> None:
        """Голос уходит теми же тремя шагами, но типом `audio`.

        Тип решает не расширение файла, а то, как Max покажет сообщение:
        `file` был бы вложением, которое надо скачивать, `audio` — записью,
        которую слушают прямо в чате.
        """
        self._upload_and_send(chat_id, path, caption, kind="audio")

    def _upload_and_send(self, chat_id: int, path, caption: str, kind: str) -> None:
        path = Path(path)
        size = path.stat().st_size
        if size > self.upload_limit:
            raise FileTooBig("файл больше, чем мост отправляет через Max",
                             size=size, limit=self.upload_limit)

        place = self._ask_for_a_place(kind)
        token = self._upload(place.get("url") or "", path) or place.get("token")
        if not token:
            raise RuntimeError("Max не вернул метку загруженного файла")

        self.sleep(UPLOAD_SETTLE)     # их сторона дожёвывает файл — дадим ей секунду
        self._send_with_attachment(chat_id, token, caption, kind=kind)

    def _ask_for_a_place(self, kind: str = "file") -> dict:
        resp = self.session.post(BASE + "/uploads", params={"type": kind},
                                 headers=self.headers, verify=self.verify, timeout=60)
        data = _json_of(resp)
        if resp.status_code >= 400 or not (data.get("url") or data.get("token")):
            raise RuntimeError("Max не дал места под файл: "
                               + self._mask(_trouble(resp, data)))
        return data

    def _upload(self, url: str, path: Path) -> str:
        if not url:
            return ""
        with open(path, "rb") as body:
            # Поле формы называется `data` — так в их примере и в рабочем коде;
            # токена в этот запрос не кладём, это уже не platform-api2.
            # Третий член — вид файла, и он обязателен: без него загрузка
            # отвечает «в запросе нет файла» (живая приёмка 15.09).
            resp = self.session.post(url,
                                     files={"data": (path.name, body, mime_of(path))},
                                     verify=self.verify, timeout=FILE_TIMEOUT)
        data = _json_of(resp)
        if resp.status_code >= 400:
            raise RuntimeError("не смог залить файл в Max: "
                               + self._mask(_trouble(resp, data)))
        return str(data.get("token") or "")

    def _send_with_attachment(self, chat_id: int, token: str, caption: str,
                              kind: str = "file") -> None:
        body = {"attachments": [{"type": kind, "payload": {"token": token}}]}
        if caption:
            body["text"] = caption[:self.limit]

        for attempt in range(1, NOT_READY_TRIES + 1):
            resp = self.session.post(BASE + "/messages", params={"chat_id": chat_id},
                                     json=body, headers=self.headers,
                                     verify=self.verify, timeout=FILE_TIMEOUT)
            data = _json_of(resp)
            if resp.status_code < 400 and data.get("code") != NOT_READY:
                return
            if data.get("code") != NOT_READY:
                raise RuntimeError("не смог отправить файл в Max: "
                                   + self._mask(_trouble(resp, data)))
            if attempt < NOT_READY_TRIES:
                # Пауза растёт: так просит их документация про «файл ещё обрабатывается».
                self.sleep(NOT_READY_STEP * attempt)

        raise RuntimeError("Max так и не дообработал файл — попробуйте ещё раз попозже")

    # --- ответ --------------------------------------------------------------

    def send(self, chat_id: int, text: str) -> None:
        parts = chunk(text, self.limit)
        for number, part in enumerate(parts):
            if number:
                # Max принимает не больше двух сообщений в секунду в один чат
                # (проверено живым ботом: перебор отвечает 429 too.many.requests).
                self.sleep(SEND_PAUSE)
            self.session.post(BASE + "/messages", params={"chat_id": chat_id},
                              json={"text": part}, headers=self.headers,
                              verify=self.verify, timeout=60)


def _is_group(recipient: dict, chat_id) -> bool:
    """Личка («dialog») или общий чат («chat»).

    Вид Max кладёт рядом с номером чата; если его нет, выдаёт отрицательный
    номер — так выглядят все групповые чаты (живая приёмка 15.09).
    """
    kind = str((recipient or {}).get("chat_type") or "").lower()
    if kind:
        return kind != "dialog"
    try:
        return int(chat_id) < 0
    except (TypeError, ValueError):
        return False


def _who(sender: dict) -> str:
    """Имя отправителя так, как его показывает Max."""
    for key in ("name", "first_name", "display_name"):
        value = str(sender.get(key) or "").strip()
        if value:
            last = str(sender.get("last_name") or "").strip()
            return f"{value} {last}".strip() if key == "first_name" and last else value
    nick = str(sender.get("username") or "").strip()
    return f"@{nick}" if nick else ""


def _attachments(raw) -> list[Attachment]:
    """Вложения Max в общем виде. Наклейки, точки на карте и визитки — не файлы."""
    found: list[Attachment] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        kind = FILE_KINDS.get(str(item.get("type") or ""))
        if kind is None:
            continue
        payload = item.get("payload") or {}
        found.append(Attachment(
            kind=kind,
            file_id=str(payload.get("token") or ""),
            # У файла имя и размер лежат рядом с payload, а не внутри него.
            file_name=str(item.get("filename") or payload.get("filename") or ""),
            size=int(item.get("size") or payload.get("size") or 0),
            # Длину записи Max кладёт рядом с вложением; её может и не быть —
            # тогда предел длины проверит уже сам распознаватель.
            duration=int(item.get("duration") or payload.get("duration") or 0),
            url=str(payload.get("url") or ""),
            raw=item,
        ))
    return found


def mime_of(path) -> str:
    """Вид файла для multipart: по расширению, а иначе — «просто байты».

    Кириллица в имени этому не мешает: `mimetypes` смотрит на хвост после
    точки, а имя целиком уезжает в заголовок части как есть, в UTF-8.
    """
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or DEFAULT_MIME


def _json_of(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _trouble(resp, data: dict) -> str:
    """Что сказал Max, одной строкой: код ошибки, текст или хотя бы номер ответа."""
    return str(data.get("code") or data.get("message") or f"код {resp.status_code}")


def _retry_after(resp) -> int | None:
    value = (resp.headers or {}).get("Retry-After")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
