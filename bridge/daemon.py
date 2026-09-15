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

from . import narrator, texts
from .receivers.base import (BridgeConflict, FileTooBig, RateLimited,
                             TokenRejected, mask)
from .router import Router
from .works import WorkPool

EXIT_OK = 0
EXIT_STALE = 3          # «мой код устарел» — юнит поднимет новую версию
NETWORK_PAUSE = 5
DEFAULT_PAUSE = 10
IDLE_PAUSE = 1          # чтобы быстрые пустые ответы не крутили цикл вхолостую

CONFLICT_TEXT = {"telegram": texts.TELEGRAM_CONFLICT, "max": texts.MAX_CONFLICT}
TOKEN_TEXT = {"telegram": texts.TELEGRAM_TOKEN_REJECTED, "max": texts.MAX_TOKEN_REJECTED}


class Bridge:
    def __init__(self, config, store, executor, receivers: dict, sleeper=time.sleep,
                 router: Router | None = None, pool: WorkPool | None = None):
        self.config = config
        self.store = store
        self.executor = executor
        self.receivers = dict(receivers)
        self.sleep = sleeper
        self.pool = pool or WorkPool(executor=executor, store=store,
                                     max_parallel=getattr(config, "parallel", 1))
        self.router = router or Router(config=config, store=store, executor=executor,
                                       pool=self.pool, postbox=self)
        # Роутеру нужен почтовый ящик, чтобы «отдай» ушло во все мессенджеры.
        if getattr(self.router, "postbox", None) is None:
            self.router.postbox = self
        # Будильник живёт в главном цикле и говорит через тот же ящик: отчёт
        # о ночной работе — это то, что мост присылает сам, значит, во все каналы.
        self.alarm = self.router.alarm
        self.alarm.postbox = self
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

        # Часы смотрим каждым заходом цикла, но сам будильник просыпается
        # не чаще, чем раз в полминуты: разбудить его чаще — гонять базу зря.
        handled += self._look_at_the_clock()
        handled += self.deliver()
        return handled

    def _look_at_the_clock(self) -> int:
        try:
            return self.alarm.tick()
        except Exception as exc:                            # noqa: BLE001
            # Расписание не имеет права ронять разговор в чате.
            self.store.note("error", text=self._mask(f"будильник споткнулся: {exc}")[:200])
            return 0

    # --- готовые работы -----------------------------------------------------

    def deliver(self) -> int:
        """Доделанные работы отвечают в чат отсюда, из главного потока.

        Из рабочих потоков в мессенджер не пишем: приёмник и его соединение
        у канала одно на всех.
        """
        delivered = 0
        for work in self.pool.collect():
            if (work.meta or {}).get("schedule_id"):
                # Работа по расписанию отчитывается во все каналы, а не в тот
                # чат, где задачу однажды завели: тишина ответом не считается.
                self._report_scheduled(work)
                delivered += 1
                continue
            receiver = self.receivers.get(work.channel)
            if receiver is None:
                continue
            try:
                answers = self.router.finished_messages(work)
            except Exception as exc:                        # noqa: BLE001
                answers = [texts.WORK_FAILED.format(error=self._mask(str(exc)))]
            if not self._say_all(receiver, work.channel, work.chat_id, answers):
                delivered += 1
                continue
            # Голосом — только если просили: «ответь голосом», «прочитай».
            # Текст уже ушёл целиком, запись читает из него первые полторы тысячи.
            try:
                extra = self.router.voice_after_work(work, answers, receiver)
            except Exception as exc:                        # noqa: BLE001
                self.store.note("error", channel=work.channel, chat_id=work.chat_id,
                                text=self._mask(f"не прочитала вслух: {exc}")[:200])
                extra = []
            self._say_all(receiver, work.channel, work.chat_id, extra)
            delivered += 1
        return delivered

    def _report_scheduled(self, work) -> None:
        """Обязательный отчёт о ночной работе. Каналов нет — молча в журнал."""
        try:
            text = self.alarm.report(work)
        except Exception as exc:                            # noqa: BLE001
            text = texts.WORK_FAILED.format(error=self._mask(str(exc)))
        try:
            if not self.broadcast(text, aloud=True):
                self.store.note("schedule", text=text[:200])
        except Exception as exc:                            # noqa: BLE001
            self.store.note("error", text=self._mask(f"не отчитался о работе: {exc}")[:200])

    def _say_all(self, receiver, channel: str, chat_id: int, answers) -> bool:
        """Отправляет готовые куски в чат. False — значит, канал не принял."""
        for answer in answers or []:
            try:
                receiver.send(chat_id, answer)
            except Exception as exc:                        # noqa: BLE001
                self.store.note("error", channel=channel, chat_id=chat_id,
                                text=self._mask(f"не смог ответить: {exc}")[:200])
                return False
        return True

    # --- сказать во все каналы разом ----------------------------------------

    def broadcast(self, text: str, file=None, aloud: bool = False) -> int:
        """То, что мост присылает сам, уходит во все настроенные мессенджеры.

        Правило про два входа целиком: спросили в одном — ответ там же, а что
        мост шлёт по своему почину (сводка, «отдай») — в оба. В каждом канале
        адресат один: самый свежий чат, а не все, где человек здоровался.

        Один упавший канал не отменяет остальные: беду пишем в журнал и идём
        дальше. Возвращаем, до скольких чатов дошло.
        """
        delivered = 0
        too_big = None
        for channel, receiver in self.receivers.items():
            chat_id = self._broadcast_chat(channel)
            if chat_id is None:
                continue
            try:
                if file is not None:
                    receiver.send_file(chat_id, file, caption=text or "")
                else:
                    for piece in narrator.chunk(text, getattr(receiver, "limit",
                                                              narrator.TELEGRAM_LIMIT)):
                        receiver.send(chat_id, piece)
                    if aloud and self._reads_aloud():
                        # Сводку читаем вслух только по настройке voice.reply:
                        # незваный голос в семь утра — это не забота.
                        self._say_all(receiver, channel, chat_id,
                                      self.router.speak(chat_id, text, receiver))
                delivered += 1
            except FileTooBig as exc:
                too_big = exc
                self.store.note("error", channel=channel, chat_id=chat_id,
                                text=f"файл не прошёл по размеру: {exc}"[:200])
            except Exception as exc:                        # noqa: BLE001
                self.store.note("error", channel=channel, chat_id=chat_id,
                                text=self._mask(f"не смог сказать во все каналы: {exc}")[:200])
        if not delivered and too_big is not None:
            # Ни один канал файла не взял по размеру — пусть наверху скажут об этом
            # человеку числами, а не общим «не вышло».
            raise too_big
        return delivered

    def _reads_aloud(self) -> bool:
        return bool(getattr(getattr(self.config, "voice", None), "reply", False))

    def _broadcast_chat(self, channel: str):
        """Куда говорить в этом канале: свежий чат, а если его нет — по списку своих.

        Запасной ход работает только для Telegram: там в личке номер чата и есть
        ваш id. У Max это разные числа, и угадывать их мост не станет.
        """
        link = self.store.newest_link_of(channel)
        if link is not None:
            return link["chat_id"]
        if channel != "telegram":
            return None
        allowed = getattr(self.config.channel(channel), "allowlist", None) or []
        return allowed[0] if allowed else None

    # --- после перезагрузки -------------------------------------------------

    def recover(self) -> int:
        """Работы, застигнутые перезагрузкой, честно объявляем прерванными.

        Связки и сессии остаются: разговор можно продолжить, а вот задание
        не доделано — и человек должен узнать об этом от моста, а не по тишине.
        """
        told = 0
        for job in self.store.mark_running_interrupted():
            receiver = self.receivers.get(job["channel"])
            head = (job["prompt_head"] or "").strip().replace("\n", " ")[:60]
            _say(f"прерванная работа {job['id']}: {head}")
            if receiver is None:
                continue
            text = texts.INTERRUPTED_BY_RESTART.format(head=head)
            limit = getattr(receiver, "limit", narrator.TELEGRAM_LIMIT)
            try:
                for piece in narrator.chunk(text, limit):
                    receiver.send(job["chat_id"], piece)
                told += 1
            except Exception as exc:                        # noqa: BLE001
                self.store.note("error", channel=job["channel"], chat_id=job["chat_id"],
                                text=self._mask(f"не сказал о прерванной работе: {exc}")[:200])
        return told

    def _answer(self, receiver, message) -> None:
        try:
            # Приёмник передаём внутрь: файл скачать и отправить умеет только он.
            answers = self.router.handle(message, receiver=receiver)
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
        self.recover()
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
        if max_ticks is not None:
            # Отладочный заход «один раз»: дожидаемся работы и отвечаем по ней.
            self.pool.wait_idle(timeout=getattr(self.config, "timeout_sec", 900) + 5)
            self.deliver()
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
