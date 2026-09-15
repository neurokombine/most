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

from pathlib import Path

import requests

from ..narrator import MAX_LIMIT, chunk
from .base import BridgeConflict, Incoming, RateLimited, Receiver, TokenRejected, mask

BASE = "https://platform-api2.max.ru"
LONG_POLL_TIMEOUT = 25
MARKER_KEY = "max_marker"
MESSAGE_UPDATES = ("message_created", "comment_created")

_BUNDLE = Path(__file__).resolve().parent.parent.parent / "certs" / "max_ca_bundle.pem"
CA_BUNDLE = str(_BUNDLE) if _BUNDLE.exists() else True


class MaxReceiver(Receiver):
    channel = "max"
    limit = MAX_LIMIT

    def __init__(self, token: str, store, session=None, long_poll_timeout: int = LONG_POLL_TIMEOUT,
                 verify=CA_BUNDLE):
        self.token = token
        self.store = store
        self.session = session or requests.Session()
        self.long_poll_timeout = long_poll_timeout
        self.verify = verify

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

        text = (body.get("text") or update.get("text") or "").strip()
        if not text:
            return None                                       # вложения — следующие этапы

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
            raw=update,
        )

    # --- ответ --------------------------------------------------------------

    def send(self, chat_id: int, text: str) -> None:
        for part in chunk(text, self.limit):
            self.session.post(BASE + "/messages", params={"chat_id": chat_id},
                              json={"text": part}, headers=self.headers,
                              verify=self.verify, timeout=60)


def _retry_after(resp) -> int | None:
    value = (resp.headers or {}).get("Retry-After")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
