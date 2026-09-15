"""Демон моста: один процесс, два приёмника, одна папка состояния.

Что здесь важно и почему (перенесено с боевого approve_bot.py):
  • три класса бед разведены по поведению, а не по номеру кода: перехваченный
    слушатель и непризнанный токен гасят свой канал (молчаливый вечный ретрай
    опаснее честной остановки), а «подождите» — это именно подождать;
  • канал гаснет свой, а не весь мост: у Max может быть всё хорошо, когда
    Telegram уже отобрали;
  • если каналов не осталось — выходим с кодом 0. Это не «всё хорошо», это
    «чинить руками, респавн не поможет»: Restart=on-failure на нулевом коде
    не поднимает, и юнит не уходит в цикл перезапусков;
  • заметили, что код моста на диске изменился, — выходим кодом 3, юнит
    поднимает уже новую версию (в юните на это SuccessExitStatus=3 и
    RestartForceExitStatus=3).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

from . import texts
from .receivers.base import BridgeConflict, RateLimited, TokenRejected, mask
from .router import Router

EXIT_OK = 0
EXIT_STALE = 3          # «мой код устарел» — юнит поднимет новую версию
NETWORK_PAUSE = 5
DEFAULT_PAUSE = 10
IDLE_PAUSE = 1          # чтобы быстрые пустые ответы не крутили цикл вхолостую

CONFLICT_TEXT = {"telegram": texts.TELEGRAM_CONFLICT, "max": texts.MAX_CONFLICT}
TOKEN_TEXT = {"telegram": texts.TELEGRAM_TOKEN_REJECTED, "max": texts.MAX_TOKEN_REJECTED}


class Bridge:
    def __init__(self, config, store, executor, receivers: dict, sleeper=time.sleep,
                 router: Router | None = None):
        self.config = config
        self.store = store
        self.executor = executor
        self.receivers = dict(receivers)
        self.sleep = sleeper
        self.router = router or Router(config=config, store=store, executor=executor)
        self._fingerprint = _code_fingerprint()

    # --- состояние ----------------------------------------------------------

    def alive(self) -> bool:
        return bool(self.receivers)

    def code_changed(self) -> bool:
        return any(_mtime(path) != mtime for path, mtime in self._fingerprint.items())

    def _stop_channel(self, channel: str, reason: str) -> None:
        self.receivers.pop(channel, None)
        self.store.note("stopped", channel=channel, text=reason)
        _say(f"{channel}: {reason}")

    # --- один заход ---------------------------------------------------------

    def tick(self) -> int:
        handled = 0
        for channel in list(self.receivers):
            receiver = self.receivers.get(channel)
            if receiver is None:
                continue
            try:
                incoming = receiver.poll_once()
            except BridgeConflict:
                self._stop_channel(channel, CONFLICT_TEXT.get(channel, texts.TELEGRAM_CONFLICT))
                continue
            except TokenRejected:
                self._stop_channel(channel, TOKEN_TEXT.get(channel, texts.TELEGRAM_TOKEN_REJECTED))
                continue
            except RateLimited as exc:
                wait = exc.retry_after or DEFAULT_PAUSE
                _say(texts.RATE_LIMITED.format(channel=channel, seconds=wait))
                self.sleep(wait)
                continue
            except requests.exceptions.RequestException as exc:
                _say(texts.NETWORK_TROUBLE.format(channel=channel))
                self.store.note("error", channel=channel,
                                text=self._mask(f"сеть: {exc}")[:200])
                self.sleep(NETWORK_PAUSE)
                continue
            except Exception as exc:                            # noqa: BLE001
                self.store.note("error", channel=channel,
                                text=self._mask(f"опрос не удался: {exc}")[:200])
                self.sleep(NETWORK_PAUSE)
                continue

            for message in incoming:
                handled += 1
                self._answer(receiver, message)
        return handled

    def _answer(self, receiver, message) -> None:
        try:
            answers = self.router.handle(message)
        except Exception as exc:                                # noqa: BLE001
            # Одно сообщение не должно ронять мост, и человек не должен видеть трассировку.
            self.store.note("error", channel=message.channel, chat_id=message.chat_id,
                            user_id=message.user_id,
                            text=self._mask(f"не обработано: {exc}")[:200])
            answers = [texts.WORK_FAILED.format(error=self._mask(str(exc)))]
        for answer in answers:
            try:
                receiver.send(message.chat_id, answer)
            except Exception as exc:                            # noqa: BLE001
                self.store.note("error", channel=message.channel, chat_id=message.chat_id,
                                text=self._mask(f"не смог ответить: {exc}")[:200])
                break

    def _mask(self, text) -> str:
        return mask(text, self.config.secrets())

    # --- вечный цикл --------------------------------------------------------

    def run(self, max_ticks: int | None = None) -> int:
        ticks = 0
        while self.alive():
            if self.code_changed():
                _say("код моста изменился — перезапускаюсь, чтобы работать новой версией")
                return EXIT_STALE
            handled = self.tick()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if not handled:
                self.sleep(IDLE_PAUSE)
        if not self.alive():
            _say("слушать больше нечего — выхожу. Почините настройку и запустите снова.")
        return EXIT_OK


def _say(text: str) -> None:
    print(f"мост: {text}", flush=True)


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _code_fingerprint() -> dict[str, float]:
    """Слепок времени правки собственных модулей: правили код — перезапускаемся."""
    out = {}
    for name, module in list(sys.modules.items()):
        if name != "bridge" and not name.startswith("bridge."):
            continue
        path = getattr(module, "__file__", None)
        if path and Path(path).exists():
            out[path] = _mtime(path)
    return out
