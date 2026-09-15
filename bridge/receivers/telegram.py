"""Приёмник Telegram: длинный опрос getUpdates с персистентным смещением.

Списано с боевого `approve_bot.py`: смещение живёт в базе (иначе после
перезапуска бот заново отвечает на вчерашнее), три класса ошибок разведены
по поведению, токен маскируется в любом тексте до печати.
"""
from __future__ import annotations

import requests

from ..narrator import TELEGRAM_LIMIT, chunk
from .base import BridgeConflict, Incoming, RateLimited, Receiver, TokenRejected, mask

API = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL_TIMEOUT = 25
OFFSET_KEY = "telegram_offset"
ALLOWED_UPDATES = ["message"]


class TelegramReceiver(Receiver):
    channel = "telegram"
    limit = TELEGRAM_LIMIT

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
        if resp.status_code in (401, 403):
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
        text = (message.get("text") or "").strip()
        if not text:
            return None                                       # голос и файлы — следующие этапы
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        return Incoming(
            channel=self.channel,
            chat_id=int(chat.get("id")),
            user_id=int(sender.get("id")),
            text=text,
            thread_id=int(message.get("message_thread_id") or 0),
            raw=update,
        )

    # --- ответ --------------------------------------------------------------

    def send(self, chat_id: int, text: str) -> None:
        for part in chunk(text, self.limit):
            self.session.post(self._url("sendMessage"),
                              data={"chat_id": chat_id, "text": part,
                                    "disable_web_page_preview": True},
                              timeout=30)


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
