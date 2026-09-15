"""Маршрутизатор: кому отвечаем, в какой папке работаем, что говорим.

Порядок один и тот же для обоих мессенджеров:
  1. свой или чужой — чужому молчим, но пишем в журнал;
  2. команда словами («покажи проекты», «стоп», «кто стучался»);
  3. иначе — задача нейросети в папке связки, ответ в тот же канал.

Ключ связки — тройка (канал, чат, тема). В личке и в Max тема всегда 0;
темы форума Telegram лягут в тот же ключ, когда до них дойдут руки.

С этапа 2 задача не выполняется прямо здесь: она уходит в очередь
(`works.WorkPool`) и работает своим потоком, а ответ в чат отправляет главный
цикл, когда работа кончится. Иначе мост глохнет на все пятнадцать минут,
пока нейросеть думает, — и не слышит даже слова «стоп».
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import alarm, changes, narrator, postman, texts, voice
from .executor import Executor
from .receivers.base import FileTooBig, Incoming, mask
from .works import WorkPool

# Пояс читается через zoneinfo (alarm.zone); запасной путь — те же +3.
MOSCOW = alarm.zone()

LIMITS = {"telegram": narrator.TELEGRAM_LIMIT, "max": narrator.MAX_LIMIT}
CHANNEL_NAMES = texts.CHANNEL_NAMES

# Ключи в settings: чем платить за две колонки в базе, если хватает двух строк.
LAST_INCOMING_KEY = "last_incoming:{link_id}"
RESULT_FILE_KEY = "result_file:{link_id}"

# «Дальше работаем с папкой бухгалтерия.» — ровно так это говорят в кадре Б.6:
# с разгонным словом впереди и с точкой в конце. Без них разбор не срабатывал,
# и фраза уходила заданием нейросети.
SWITCH_RE = re.compile(r"^\s*(?:дальше|теперь|а\s+теперь|давай|итак)?[\s,]*"
                       r"(?:работаем|работай|переходим|перейди|перейдём|перейдем)\s+"
                       r"(?:с|со|в|на)\s+(?:папкой\s+|папку\s+|проектом\s+|проект\s+)?"
                       r"[«\"']?(?P<name>[^«»\"']+?)[»\"']?\s*[.!]?\s*$",
                       re.IGNORECASE)
KNOCKS_RE = re.compile(r"кто\s+(?:ко\s+мне\s+)?стуч", re.IGNORECASE)
STOP_RE = re.compile(r"^\s*(?:стоп|хватит|прерви(?:сь)?|останови(?:сь)?|отмени|"
                     r"остановись,?\s+пожалуйста)\s*[.!]?\s*$", re.IGNORECASE)
NEW_SESSION_RE = re.compile(r"нов(?:ый|ая)\s+(?:разговор|сесси|беседа)|"
                            r"начн(?:и|ём|ем)\s+(?:заново|с\s+чистого)|"
                            r"забудь\s+(?:всё|все|прошл)", re.IGNORECASE)
CHANGES_RE = re.compile(r"(?:что\s+(?:там\s+)?получилось|что\s+изменилось|"
                        r"что\s+появилось|покажи\s+результат)", re.IGNORECASE)
HISTORY_RE = re.compile(r"что\s+ты\s+(?:делал|сделал)|"
                        r"(?:покажи|последние)\s+(?:мои\s+)?(?:работ|задач)|"
                        r"чем\s+ты\s+занимал", re.IGNORECASE)
# «Покажи, какие у меня есть папки» — между словом-просьбой и словом «папки»
# у человека помещается что угодно: запятая, «у меня», «есть». Держим окно,
# а не жёсткий пробел.
PROJECTS_RE = re.compile(r"(?:покажи|какие|список|перечисли)\b[\s,]*"
                         r"(?:мне\s+|у\s+меня\s+|есть\s+|мои\s+|все\s+|какие\s+)*"
                         r"(?:проект|папк|папок)", re.IGNORECASE)
# «Отдай» — про результат последней работы; отдельно и строго, чтобы не съесть
# «отдай мне смету»: там человек называет файл, и это уже другая команда.
RESULT_RE = re.compile(r"^\s*(?:отдай|отдать|отдавай|"
                       r"(?:пришли|скинь|отправь|вышли)\s+(?:мне\s+)?"
                       r"(?:результат|что\s+получилось))\s*[.!]?\s*$", re.IGNORECASE)
SEND_FILE_RE = re.compile(r"^\s*(?:пришли|при[сш]ылай|отправь|скинь|кинь|вышли|отдай|дай)\s+"
                          r"(?P<name>.+?)\s*[.!?]?\s*$", re.IGNORECASE)
WHERE_FILE_RE = re.compile(r"куда\s+(?:ты\s+)?(?:её|его|их)?\s*"
                           r"(?:положила|поло[жд]ил|сохранила|дела|убрала)", re.IGNORECASE)
# Слова, которые в просьбе «пришли мне …» именем файла не являются.
FILE_STOPWORDS = {"мне", "сюда", "нам", "файл", "файлик", "документ", "этот", "тот",
                  "эту", "ту", "это", "его", "её", "ее", "их", "пожалуйста", "плз",
                  "ещё", "еще", "раз", "обратно", "назад", "в", "чат", "скорее"}
# А эти — вовсе не про файлы: «пришли ответ целиком ещё раз» — это про сообщение.
# «Сводка» здесь тоже не имя файла: «пришли сводку» — это просьба к мосту
# рассказать, что он делал за сутки (такт 8 урока), и она разбирается ниже.
NOT_A_FILE = {"ответ", "ответы", "сообщение", "сообщения", "текст", "смс", "письмо",
              "сводку", "сводка", "сводки"}
HELP_RE = re.compile(r"^\s*(?:/?help|/start|помощь|что\s+ты\s+умеешь)\s*$", re.IGNORECASE)

# --- этап 5: расписание -----------------------------------------------------
# Сама фраза «каждое утро в 7:30 …» разбирается в alarm.py; здесь — команды
# вокруг неё. Номер задачи — это её номер в базе, он не съезжает после того,
# как соседнюю убрали.
SCHEDULE_SHOW_RE = re.compile(r"расписани", re.IGNORECASE)
SCHEDULE_REMOVE_RE = re.compile(r"^\s*(?:убери|удали|сотри|отмени)\s+задачу\s+(?P<n>\d+)",
                                re.IGNORECASE)
SCHEDULE_OFF_RE = re.compile(r"^\s*(?:выключи|приостанови|останови)\s+задачу\s+(?P<n>\d+)",
                             re.IGNORECASE)
SCHEDULE_ON_RE = re.compile(r"^\s*(?:включи|верни)\s+задачу\s+(?P<n>\d+)", re.IGNORECASE)
SCHEDULE_TIME_RE = re.compile(r"(?:помен|перенес|сдвинь|поставь).{0,30}?"
                              r"задач\w*\s+(?P<n>\d+)\s+на\s+(?P<time>.+)$", re.IGNORECASE)
SCHEDULE_RUN_RE = re.compile(r"запусти\s+задачу\s+(?P<n>\d+)", re.IGNORECASE)
NIGHT_RE = re.compile(r"что\s+(?:ты\s+)?запускал|запускал\w*\s+ночью|"
                      r"что\s+было\s+ночью|по\s+расписанию\s+за\s+сутки", re.IGNORECASE)
SUMMARY_NOW_RE = re.compile(r"(?:пришли|покажи|дай|сделай|собери)\s+(?:мне\s+)?сводку|"
                            r"сводку\s+(?:сейчас|прямо\s+сейчас)", re.IGNORECASE)

# --- голос ------------------------------------------------------------------
# Что считаем записанной речью, а не файлом: голосовое Telegram, кружок и
# аудио-вложение обоих мессенджеров. Такое не ложится документом в «входящие»,
# а расшифровывается и становится заданием.
VOICE_KINDS = ("voice", "audio", "video_note")
# Голосом наружу — только по просьбе. Слово «прочитай» тут не про чтение файла:
# «прочитай, что получилось» человек говорит, когда хочет услышать ответ.
SPEAK_RE = re.compile(r"ответь\s+голосом|скажи\s+голосом|голосом\s+ответь|"
                      r"озвучь|проговори|прочитай\b|прочти\b", re.IGNORECASE)
# Про ненастроенный голос наружу говорим один раз на экземпляр, а не каждый ответ.
VOICE_OUT_TOLD_KEY = "voice_out_told"


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


def moscow_from_epoch(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, MOSCOW).strftime("%d.%m %H:%M МСК")


def epoch_of(raw: str) -> float:
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        return 0.0


# «Сколько шла работа» словами — один на весь пакет, чтобы чат и сводка
# считали одинаково.
how_long = alarm.how_long


class Router:
    def __init__(self, config, store, executor: Executor, pool: WorkPool | None = None,
                 postbox=None, ears=None, mouth=None, scheduler=None):
        self.config = config
        self.store = store
        self.executor = executor
        # Слух и голос — за интерфейсами: в тестах подставные, в бою
        # faster-whisper и piper, а если их нет — честная деградация в текст.
        built_ears, built_mouth = voice.build(config)
        self.ears = ears if ears is not None else built_ears
        self.mouth = mouth if mouth is not None else built_mouth
        # Почтовый ящик — мост целиком: через него уходит то, что мост шлёт сам,
        # во все настроенные мессенджеры разом (сводки и «отдай»).
        self.postbox = postbox
        self.pool = pool or WorkPool(executor=executor, store=store,
                                     max_parallel=getattr(config, "parallel", 1))
        # Будильник берёт ту же очередь работ: своей заводить нельзя, иначе
        # в одной папке окажутся две нейросети, а «стоп» погасит только одну.
        self.alarm = scheduler or alarm.Scheduler(config=config, store=store,
                                                  pool=self.pool, postbox=postbox)

    def _mask(self, text) -> str:
        """Ни один текст беды не уходит в журнал с токеном внутри.

        Сеть любит подставить в ошибку полный адрес запроса, а в нём у Telegram
        живёт токен бота. Журнал ученик показывает нейросети, а иногда и чужому
        человеку, — значит, чистим до записи, а не после.
        """
        return mask(text, self.config.secrets() if hasattr(self.config, "secrets") else [])

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

    def handle(self, incoming: Incoming, receiver=None) -> list[str]:
        """Возвращает готовые сообщения в тот же канал. Пустой список = молчим.

        `receiver` нужен там, где ответ — не текст, а файл: скачать присланное
        и отправить своё умеет только он. Зовётся из главного потока.
        """
        text = (incoming.text or "").strip()

        if not self.store.is_allowed(incoming.channel, incoming.user_id):
            # Молчим в чат, но пишем в журнал: без записи молчание неотличимо от поломки.
            self.store.note_stranger(incoming.channel, incoming.chat_id,
                                     incoming.user_id, text,
                                     name=getattr(incoming, "name", "") or "")
            return []

        limit = LIMITS.get(incoming.channel, narrator.TELEGRAM_LIMIT)

        attachments = list(getattr(incoming, "attachments", None) or [])
        spoken = [a for a in attachments if getattr(a, "kind", "") in VOICE_KINDS]
        if spoken:
            return self._incoming_voice(incoming, spoken[0], receiver, limit)
        if attachments:
            return self._incoming_files(incoming, receiver, limit)

        if not text:
            return []

        return self._words(incoming, text, limit, receiver)

    def _words(self, incoming: Incoming, text: str, limit: int, receiver=None) -> list[str]:
        """Разбор словами. Отдельно от `handle`, потому что сюда же приходит
        расшифрованное голосовое: «стоп», сказанное вслух, должно останавливать
        работу, а не уходить заданием нейросети."""
        if WHERE_FILE_RE.search(text):
            return narrator.chunk(self._where_is_the_file(incoming), limit)
        if RESULT_RE.match(text):
            return narrator.chunk(self._give_result(incoming, receiver), limit)
        send_file = SEND_FILE_RE.match(text)
        if send_file and _looks_like_a_file_request(send_file.group("name")):
            return narrator.chunk(
                self._send_named_file(incoming, receiver, send_file.group("name")), limit)

        if HELP_RE.match(text):
            return narrator.chunk(texts.HELP, limit)
        if STOP_RE.match(text):
            return narrator.chunk(self._stop(incoming), limit)
        if NEW_SESSION_RE.search(text):
            return narrator.chunk(self._new_session(incoming), limit)
        schedule = self._schedule_words(incoming, text, limit)
        if schedule is not None:
            return schedule
        if CHANGES_RE.search(text):
            return narrator.chunk(self._changes(incoming), limit)
        if HISTORY_RE.search(text):
            return narrator.chunk(self._history(incoming), limit)
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

    # --- этап 2: остановка, новый разговор, что получилось, что делала ------

    def key_of(self, incoming: Incoming):
        return (incoming.channel, int(incoming.chat_id), int(incoming.thread_id))

    def _stop(self, incoming: Incoming) -> str:
        """«Стоп»: гасим работу этого чата — и ночную в той же папке.

        Находка этапа 2: «стоп» останавливал только работу своей связки.
        Задачу расписания завели в одном мессенджере, «стоп» сказали в другом —
        и она продолжала жечь подписку. Человек видит папку, а не ключи связок,
        поэтому гасим по папке и вслух называем, что именно остановили.
        """
        key = self.key_of(incoming)
        mine = self.pool.stop(key)
        _link, workdir = self._workdir_of(incoming)
        others = self.pool.stop_in_dir(workdir, skip=key) if workdir else []

        if mine is None and not others:
            return texts.STOP_NOTHING_TO_STOP
        if not others:
            return ""      # молчим: через секунду придёт «остановила» и что успела

        lines = [texts.STOP_WHAT_HEADER]
        if mine is not None:
            lines.append(texts.STOP_LINE_MINE.format(prompt=(mine.prompt or "")[:60]))
        for work in others:
            meta = work.meta or {}
            lines.append(texts.STOP_LINE_SCHEDULE.format(
                number=meta.get("schedule_id", "?"),
                prompt=(meta.get("schedule_prompt") or work.prompt or "")[:60]))
        return "\n".join(lines)

    def _new_session(self, incoming: Incoming) -> str:
        link = self._link_with_project(incoming)
        if link is None:
            return texts.PROJECT_NONE
        if self.pool.busy(self.key_of(incoming)):
            return texts.ALREADY_WORKING
        self.store.reset_session(link["id"], str(uuid.uuid4()))
        return texts.SESSION_RESET.format(project=link["project"])

    def _changes(self, incoming: Incoming) -> str:
        """Показывает папку, а не отчёт: это и есть проверка «отчёт ≠ результат»."""
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        job = self.store.last_finished_job(link["id"]) if link else None
        if link is None or job is None or not link["project"]:
            return texts.CHANGES_NO_WORK

        since = self._job_start_epoch(job)
        workdir = Path(self.config.projects_dir) / link["project"]
        found = changes.changed_files(workdir, since=since, limit=changes.LIMIT)
        when = moscow_from_epoch(since) if since else to_moscow(job["started_at"])
        if not found:
            return texts.CHANGES_EMPTY.format(when=when, project=link["project"])

        lines = [texts.CHANGES_HEADER.format(when=when, project=link["project"])]
        lines += [texts.CHANGES_LINE.format(name=name, at=moscow_from_epoch(mtime))
                  for name, mtime in found]
        if len(found) >= changes.LIMIT:
            lines.append(texts.CHANGES_MORE.format(limit=changes.LIMIT))
        lines.append("")
        lines.append(texts.CHANGES_FOOTER)
        return "\n".join(lines)

    @staticmethod
    def _job_start_epoch(job) -> float:
        """Метка отсчёта — НАЧАЛО последней работы.

        В meta.json смотрим поле started_at, а не время правки самого файла:
        файл переписывается на финише работы, и всё, что она успела сделать,
        оказалось бы «старее» его — список менялся бы пустым.
        """
        job_dir = job["dir"] or ""
        if job_dir:
            meta = Path(job_dir) / "meta.json"
            try:
                started = json.loads(meta.read_text(encoding="utf-8")).get("started_at")
                if started:
                    return epoch_of(started)
            except (OSError, ValueError, AttributeError):
                pass
        return epoch_of(job["started_at"])

    def _history(self, incoming: Incoming) -> str:
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        rows = self.store.jobs_of_link(link["id"], limit=5) if link else []
        if not rows:
            return texts.JOBS_EMPTY
        lines = [texts.JOBS_HEADER]
        for row in rows:
            lines.append(texts.JOB_LINE.format(
                at=to_moscow(row["started_at"]),
                how_long=how_long(row["duration_sec"]),
                outcome=texts.JOB_OUTCOME.get(row["state"], row["state"]),
                prompt=(row["prompt_head"] or "")[:60]))
        return "\n".join(lines)

    # --- этап 5: расписание словами -----------------------------------------

    def _schedule_words(self, incoming: Incoming, text: str, limit: int):
        """Всё про расписание в одном месте. None — значит, это не про него.

        Порядок важен: сначала постановка задачи («каждое утро в 7:30 …»),
        потом команды вокруг неё. Иначе «поставь на расписание: каждый день…»
        показало бы список вместо того, чтобы завести задачу.
        """
        if alarm.looks_like_schedule(text):
            return narrator.chunk(self._schedule_add(incoming, text), limit)

        for pattern, handler in (
            (SCHEDULE_REMOVE_RE, self._schedule_remove),
            (SCHEDULE_OFF_RE, lambda n: self._schedule_switch(n, False)),
            (SCHEDULE_ON_RE, lambda n: self._schedule_switch(n, True)),
            (SCHEDULE_RUN_RE, self._schedule_run),
        ):
            found = pattern.search(text)
            if found:
                return narrator.chunk(handler(int(found.group("n"))), limit)

        moved = SCHEDULE_TIME_RE.search(text)
        if moved:
            return narrator.chunk(
                self._schedule_move(int(moved.group("n")), moved.group("time")), limit)

        if NIGHT_RE.search(text):
            return narrator.chunk(self.alarm.night_text(), limit)
        if SUMMARY_NOW_RE.search(text):
            return narrator.chunk(self._summary_now(), limit)
        if SCHEDULE_SHOW_RE.search(text):
            return narrator.chunk(self._schedule_list(), limit)
        return None

    def _schedule_add(self, incoming: Incoming, text: str) -> str:
        link = self._link_with_project(incoming)
        if link is None:
            return texts.PROJECT_NONE

        spec, prompt = alarm.parse(text)
        if spec is None:
            # Переспрашиваем одной фразой с примером — гадать, что человек имел
            # в виду, в расписании нельзя: ошибка вылезет ночью и молча.
            return texts.SCHEDULE_NOT_UNDERSTOOD
        if not prompt:
            return texts.SCHEDULE_NO_PROMPT

        row = self.alarm.add(link, spec, prompt, project=link["project"])
        return texts.SCHEDULE_ADDED.format(
            number=row["id"], when=spec.human(), prompt=prompt,
            project=link["project"], next=self._when_of(row))

    def _schedule_list(self) -> str:
        rows = self.store.list_schedule()
        if not rows:
            return texts.SCHEDULE_EMPTY
        lines = [texts.SCHEDULE_HEADER]
        for row in rows:
            spec = alarm.Spec.stored(row["spec"])
            when = spec.human() if spec else row["spec"]
            shape = texts.SCHEDULE_LINE if row["enabled"] else texts.SCHEDULE_LINE_OFF
            lines.append(shape.format(number=row["id"], when=when,
                                      next=self._when_of(row),
                                      prompt=alarm.short(row["prompt"] or "")))
        lines.append("")
        # Пример в подсказке — с настоящим номером: «задача 2» при одной задаче
        # человека только собьёт.
        lines.append(texts.SCHEDULE_FOOTER.format(number=rows[0]["id"]))
        return "\n".join(lines)

    def _schedule_remove(self, number: int) -> str:
        row = self.store.get_schedule(number)
        if row is None:
            return texts.SCHEDULE_NO_SUCH.format(number=number)
        spec = alarm.Spec.stored(row["spec"])
        self.store.remove_schedule(number)
        return texts.SCHEDULE_REMOVED.format(
            number=number, when=spec.human() if spec else row["spec"])

    def _schedule_switch(self, number: int, on: bool) -> str:
        row = self.store.get_schedule(number)
        if row is None:
            return texts.SCHEDULE_NO_SUCH.format(number=number)
        self.store.enable_schedule(number, on)
        if not on:
            return texts.SCHEDULE_SWITCHED_OFF.format(number=number)
        spec = alarm.Spec.stored(row["spec"])
        if spec is not None:
            self.store.set_schedule_next(
                number, alarm.to_iso(alarm.next_run(spec, self.alarm.now(), self.alarm.tz)))
        return texts.SCHEDULE_SWITCHED_ON.format(
            number=number, next=self._when_of(self.store.get_schedule(number)))

    def _schedule_move(self, number: int, raw_time: str) -> str:
        row = self.store.get_schedule(number)
        if row is None:
            return texts.SCHEDULE_NO_SUCH.format(number=number)
        clock = alarm.parse_time(raw_time)
        if clock is None:
            return texts.SCHEDULE_TIME_UNCLEAR.format(number=number)

        spec = alarm.Spec.stored(row["spec"])
        if spec is None:
            return texts.SCHEDULE_NO_SUCH.format(number=number)
        moved = alarm.Spec(spec.kind, clock[0], clock[1], spec.weekday)
        self.store.set_schedule_spec(
            number, moved.to_stored(),
            next_run_at=alarm.to_iso(alarm.next_run(moved, self.alarm.now(), self.alarm.tz)))
        return texts.SCHEDULE_MOVED.format(
            number=number, when=moved.human(),
            next=self._when_of(self.store.get_schedule(number)))

    def _schedule_run(self, number: int) -> str:
        """«Запусти задачу 2 сейчас» — проверка руками, расписание не двигается."""
        row = self.store.get_schedule(number)
        if row is None:
            return texts.SCHEDULE_NO_SUCH.format(number=number)
        if self.alarm.launch(row, move_next=False) is None:
            return texts.SCHEDULE_RUN_BUSY
        return texts.SCHEDULE_RUN_NOW.format(number=number)

    def _summary_now(self) -> str:
        """Сводка по просьбе. Ушла во все каналы — в этом чате молчим."""
        text = self.alarm.summary_text(self.alarm.now())
        if self.postbox is not None and self.alarm.announce(text):
            return ""
        return text

    def _when_of(self, row) -> str:
        when = alarm.from_iso(row["next_run_at"], self.alarm.tz) if row["next_run_at"] else None
        return alarm.when_text(when, self.alarm.tz) if when else "—"

    # --- этап 3: файлы туда и обратно ---------------------------------------

    def _workdir_of(self, incoming: Incoming):
        """Папка проекта этой связки. None — значит, работать негде."""
        link = self._link_with_project(incoming)
        if link is None:
            return None, None
        return link, Path(self.config.projects_dir) / link["project"]

    def _incoming_files(self, incoming: Incoming, receiver, limit: int) -> list[str]:
        """Присланное человеком: кладём в «входящие», подпись — это задание."""
        link, workdir = self._workdir_of(incoming)
        if link is None:
            return narrator.chunk(texts.PROJECT_NONE, limit)
        if receiver is None:                       # некому качать — честно молчим в журнал
            self.store.note("error", channel=incoming.channel, chat_id=incoming.chat_id,
                            text="файл пришёл, а приёмника нет — забрать нечем")
            return []

        said: list[str] = []
        saved: list[Path] = []
        channel = CHANNEL_NAMES.get(incoming.channel, incoming.channel)
        for attachment in incoming.attachments:
            name = attachment.file_name or "файл"
            try:
                body = receiver.fetch(attachment)
            except FileTooBig as too_big:
                said.append(texts.FILE_TOO_BIG_IN.format(
                    name=name, channel=channel,
                    size=postman.human_size(too_big.size or attachment.size),
                    limit=postman.human_size(too_big.limit or receiver.download_limit)))
                continue
            except Exception as exc:               # noqa: BLE001
                self.store.note("error", channel=incoming.channel,
                                chat_id=incoming.chat_id, text=self._mask(exc)[:200])
                said.append(texts.FILE_NOT_TAKEN.format(name=name))
                continue

            path = postman.save_incoming(workdir, attachment.file_name, body,
                                         kind=attachment.kind)
            saved.append(path)
            self.store.set_setting(LAST_INCOMING_KEY.format(link_id=link["id"]), str(path))
            said.append(texts.FILE_SAVED.format(folder=postman.INBOX, name=path.name,
                                                size=postman.human_size(len(body))))

        caption = (incoming.text or "").strip()
        if saved and caption:
            # Подпись к файлу — это задание про него: запускаем работу сразу.
            prompt = texts.FILE_TASK_PROMPT.format(caption=caption, path=saved[-1])
            said += self._work(incoming, prompt, limit)
        elif saved:
            said.append(texts.FILE_SAVED_WHERE.format(path=saved[-1]))

        return narrator.chunk("\n\n".join(s for s in said if s), limit)

    # --- этап 4: голос ------------------------------------------------------

    def _incoming_voice(self, incoming: Incoming, attachment, receiver, limit: int) -> list[str]:
        """Голосовое: сохранить, расшифровать на самой машине, работать как обычно.

        Расшифровка идёт прямо здесь, в главном потоке: тридцать секунд речи —
        это двадцать секунд ожидания (замер на четырёх ядрах). Уводить её в
        рабочий поток нельзя, потому что дальше расшифровка может оказаться
        командой «стоп», а не заданием.
        """
        link, workdir = self._workdir_of(incoming)
        if link is None:
            return narrator.chunk(texts.PROJECT_NONE, limit)
        if receiver is None:
            self.store.note("error", channel=incoming.channel, chat_id=incoming.chat_id,
                            text="голосовое пришло, а приёмника нет — забрать нечем")
            return []

        max_seconds = int(getattr(getattr(self.config, "voice", None), "max_seconds",
                                  voice.MAX_SECONDS) or voice.MAX_SECONDS)
        # Длину Telegram называет сразу — значит, длинное можно отвергнуть,
        # не тратя ни трафика, ни минут расшифровки.
        if attachment.duration and attachment.duration > max_seconds:
            return narrator.chunk(texts.VOICE_TOO_LONG, limit)

        channel = CHANNEL_NAMES.get(incoming.channel, incoming.channel)
        try:
            body = receiver.fetch(attachment)
        except FileTooBig as too_big:
            return narrator.chunk(texts.FILE_TOO_BIG_IN.format(
                name="запись", channel=channel,
                size=postman.human_size(too_big.size or attachment.size),
                limit=postman.human_size(too_big.limit or receiver.download_limit)), limit)
        except Exception as exc:                        # noqa: BLE001
            self.store.note("error", channel=incoming.channel, chat_id=incoming.chat_id,
                            text=self._mask(exc)[:200])
            return narrator.chunk(texts.FILE_NOT_TAKEN.format(name="запись"), limit)

        path = postman.save_incoming(workdir, attachment.file_name, body,
                                     kind=attachment.kind or "voice",
                                     subdir=postman.VOICE_DIR)
        self.store.set_setting(LAST_INCOMING_KEY.format(link_id=link["id"]), str(path))

        if not self.ears.available():
            return narrator.chunk(texts.VOICE_NOT_SET_UP + "\n\n"
                                  + texts.VOICE_SAVED_WHERE.format(path=path), limit)

        try:
            said = self.ears.transcribe(path, max_seconds=max_seconds)
        except voice.VoiceTooLong:
            return narrator.chunk(texts.VOICE_TOO_LONG, limit)
        except voice.VoiceNotSetUp:
            return narrator.chunk(texts.VOICE_NOT_SET_UP + "\n\n"
                                  + texts.VOICE_SAVED_WHERE.format(path=path), limit)
        except Exception as exc:                        # noqa: BLE001
            self.store.note("error", channel=incoming.channel, chat_id=incoming.chat_id,
                            text=self._mask(f"не расшифровала: {exc}")[:200])
            return narrator.chunk(texts.VOICE_NOT_HEARD, limit)

        said = (said or "").strip()
        if not said:
            return narrator.chunk(texts.VOICE_NOT_HEARD, limit)

        # Сначала — что услышала, и только потом работа: человек должен увидеть
        # свои слова раньше, чем ждать полминуты ответа не на тот вопрос.
        heard = narrator.chunk(texts.VOICE_HEARD.format(text=voice.shorten(said)), limit)
        caption = (incoming.text or "").strip()
        task = f"{caption}\n\n{said}" if caption else said
        return heard + self._words(incoming, task, limit, receiver)

    # --- голос наружу -------------------------------------------------------

    def voice_after_work(self, work, answers, receiver) -> list[str]:
        """Читает ответ вслух, если об этом просили. Зовёт главный цикл после текста."""
        if not (work.meta or {}).get("voice"):
            return []
        text = "\n\n".join(a for a in answers if a)
        return self.speak(work.chat_id, text, receiver)

    def speak(self, chat_id: int, text: str, receiver) -> list[str]:
        """Отправляет текст голосом. Возвращает, что ещё сказать словами."""
        if receiver is None or not (text or "").strip():
            return []
        if not self.mouth.available():
            if self.store.get_setting(VOICE_OUT_TOLD_KEY):
                return []
            self.store.set_setting(VOICE_OUT_TOLD_KEY, "1")
            return [texts.VOICE_OUT_NOT_SET_UP]

        record = None
        try:
            record = self.mouth.say(text, self._voice_out_dir())
            # ogg/opus — голосовое сообщение; нет ffmpeg — уйдёт обычным аудио.
            sound = voice.to_ogg(record) or record
            receiver.send_voice(chat_id, sound, caption="")
        except voice.VoiceNotSetUp:
            return [texts.VOICE_OUT_NOT_SET_UP]
        except Exception as exc:                        # noqa: BLE001
            self.store.note("error", chat_id=chat_id, text=self._mask(exc)[:200])
            return [texts.VOICE_NOT_SENT]
        finally:
            _clean_up(record)
        return []

    def _voice_out_dir(self) -> Path:
        folder = Path(getattr(self.config, "home", Path.cwd())) / "voice-out"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _where_is_the_file(self, incoming: Incoming) -> str:
        """«Куда ты положила то, что я прислала» — мост отвечает сам, по своей записи."""
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        path = self.store.get_setting(
            LAST_INCOMING_KEY.format(link_id=link["id"])) if link else None
        if not path:
            return texts.WHERE_IS_THE_FILE_NONE.format(folder=postman.INBOX)
        return texts.WHERE_IS_THE_FILE.format(path=path)

    def _send_named_file(self, incoming: Incoming, receiver, name: str) -> str:
        """«Пришли мне отчёт»: ищет сам мост, без нейросети."""
        link, workdir = self._workdir_of(incoming)
        if link is None:
            return texts.PROJECT_NONE

        query = _file_query(name)
        found = postman.find_files(workdir, query) if query else []
        if len(found) == 1:
            return self._hand_over(incoming, receiver, found[0], workdir,
                                   texts.HERE_IS_RESULT.format(name=found[0].name))
        if len(found) > 1:
            return texts.FILE_WHICH_ONE.format(found=_file_lines(found, workdir))

        recent = postman.recent_files(workdir)
        if not recent:
            return texts.FILE_FOLDER_EMPTY.format(project=link["project"])
        if not query:
            return texts.FILE_WHICH_ONE_EXACTLY.format(found=_file_lines(recent, workdir))
        return texts.FILE_NOT_FOUND.format(project=link["project"],
                                           found=_file_lines(recent, workdir))

    def _give_result(self, incoming: Incoming, receiver) -> str:
        """«Отдай»: последний файл, который нейросеть назвала и правда изменила."""
        link, workdir = self._workdir_of(incoming)
        if link is None:
            return texts.PROJECT_NONE
        raw = self.store.get_setting(RESULT_FILE_KEY.format(link_id=link["id"]))
        if not raw:
            return texts.RESULT_FILE_UNKNOWN
        path = Path(raw)
        if not path.exists():
            return texts.RESULT_FILE_GONE.format(name=path.name)
        return self._hand_over(incoming, receiver, path, workdir,
                               texts.HERE_IS_RESULT.format(name=path.name), everywhere=True)

    def _hand_over(self, incoming: Incoming, receiver, path: Path, workdir,
                   caption: str, everywhere: bool = False) -> str:
        """Отдаёт файл человеку. Спросили в одном — отвечаем там же.

        «Отдай» — другое дело: это то, что мост присылает сам, и уходит оно
        во все настроенные мессенджеры (решение про два входа).
        """
        channel = CHANNEL_NAMES.get(incoming.channel, incoming.channel)
        if everywhere and self.postbox is not None:
            try:
                if self.postbox.broadcast(caption, file=path):
                    return ""
            except FileTooBig as too_big:
                return texts.FILE_TOO_BIG_OUT.format(
                    name=path.name, channel=channel,
                    size=postman.human_size(too_big.size or postman.size_of(path)),
                    limit=postman.human_size(too_big.limit), path=path)
            except Exception as exc:                # noqa: BLE001
                self.store.note("error", channel=incoming.channel,
                                text=self._mask(exc)[:200])
                return texts.FILE_NOT_SENT_WHY.format(path=path,
                                                      trouble=self._mask(exc)[:200])
            return texts.FILE_NOT_SENT.format(path=path)

        if receiver is None:
            return texts.FILE_NOT_SENT.format(path=path)
        try:
            receiver.send_file(incoming.chat_id, path, caption=caption)
        except FileTooBig as too_big:
            return texts.FILE_TOO_BIG_OUT.format(
                name=path.name, channel=channel,
                size=postman.human_size(too_big.size or postman.size_of(path)),
                limit=postman.human_size(too_big.limit or receiver.upload_limit),
                path=path)
        except Exception as exc:                    # noqa: BLE001
            # Причину пишем и в журнал, и человеку: живая приёмка 15.09 показала,
            # что «не вышло» без причины нечего даже переслать за помощью.
            self.store.note("error", channel=incoming.channel, chat_id=incoming.chat_id,
                            text=self._mask(exc)[:200])
            return texts.FILE_NOT_SENT_WHY.format(path=path,
                                                  trouble=self._mask(exc)[:200])
        return ""                                   # файл ушёл, подпись при нём

    # --- работа -------------------------------------------------------------

    def _link_with_project(self, incoming: Incoming):
        """Связка с папкой: заводим при первом сообщении, папка — текущая или первая."""
        link = self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)
        if link is not None and link["project"]:
            return link
        project = self.default_project()
        if project is None:
            return None
        self.store.upsert_link(incoming.channel, incoming.chat_id, incoming.thread_id,
                               project=project)
        self.store.set_link_project(incoming.channel, incoming.chat_id,
                                    incoming.thread_id, project)
        return self.store.get_link(incoming.channel, incoming.chat_id, incoming.thread_id)

    def _work(self, incoming: Incoming, text: str, limit: int) -> list[str]:
        """Ставит задачу в очередь. Ответ с результатом придёт отдельным сообщением."""
        link = self._link_with_project(incoming)
        if link is None:
            return narrator.chunk(texts.PROJECT_NONE, limit)

        key = self.key_of(incoming)
        if self.pool.busy(key):
            return narrator.chunk(texts.ALREADY_WORKING, limit)
        if not self.pool.has_free_slot():
            return narrator.chunk(
                texts.ALL_HANDS_BUSY.format(parallel=self.pool.max_parallel), limit)

        workdir = Path(self.config.projects_dir) / link["project"]
        work = self.pool.submit(link=link, channel=incoming.channel,
                                chat_id=incoming.chat_id, thread_id=incoming.thread_id,
                                prompt=text, workdir=workdir,
                                resume=bool(link["session_started"]))
        if work is None:                      # кто-то успел раньше на доли секунды
            return narrator.chunk(texts.ALREADY_WORKING, limit)
        # «Ответь голосом» относится к этой работе и только к ней: следующая
        # задача снова отвечает текстом, пока не попросят вслух ещё раз.
        work.meta["voice"] = bool(SPEAK_RE.search(text))
        return narrator.chunk(texts.WORK_ACCEPTED, limit)

    # --- ответ, когда работа кончилась --------------------------------------

    def finished_messages(self, work) -> list[str]:
        """Что сказать в чат про доделанную работу. Зовёт главный цикл."""
        limit = LIMITS.get(work.channel, narrator.TELEGRAM_LIMIT)
        result = work.result
        parts: list[str] = []
        if work.session_restarted:
            parts.append(texts.SESSION_LOST_NEW)

        if result is None:
            parts.append(texts.WORK_EMPTY_ANSWER)
        elif result.stopped:
            parts.append(texts.STOPPED_BY_HAND)
            parts.append(self._managed(result))
        elif result.timed_out:
            budget = how_long(getattr(self.executor, "timeout", 900))
            parts.append(texts.WORK_TIMED_OUT.format(budget=budget))
            parts.append(self._managed(result))
        elif not result.ok:
            self.store.note("error", channel=work.channel, chat_id=work.chat_id,
                            text=(result.error or "")[:200])
            parts.append(texts.WORK_FAILED.format(
                error=result.error or "работа завершилась неудачно"))
        else:
            parts.append(result.text or narrator.final_text(result.events)
                         or texts.WORK_EMPTY_ANSWER)
            hint = self._remember_result_file(work, result)
            if hint:
                parts.append(hint)

        return narrator.chunk("\n\n".join(p for p in parts if p), limit) \
            or [texts.WORK_EMPTY_ANSWER]

    def _remember_result_file(self, work, result) -> str:
        """Если нейросеть назвала файл и он правда изменился — запоминаем его.

        Дальше хватает одного слова «отдай»: мост знает, что присылать.
        Проверяем не словам, а папке — тем же `changes.py`, что и
        «покажи, что получилось».
        """
        try:
            found = changes.mentioned_files(result.text or "", work.workdir,
                                            since=work.started_wall)
        except Exception:                           # noqa: BLE001
            return ""
        if not found:
            return ""
        self.store.set_setting(RESULT_FILE_KEY.format(link_id=work.link_id), str(found[0]))
        return texts.RESULT_FILE_HINT.format(name=found[0].name)

    @staticmethod
    def _managed(result) -> str:
        partial = (result.partial or "").strip()
        return texts.WHAT_MANAGED.format(partial=partial) if partial else texts.NOTHING_MANAGED


def _file_query(name: str) -> str:
    """Из просьбы «пришли мне этот файл» остаётся пусто, из «пришли смету» — «смету»."""
    words = [w for w in re.split(r"\s+", (name or "").strip().strip("«»\"'")) if w]
    kept = [w for w in words if w.lower().strip(".,!?«»\"'") not in FILE_STOPWORDS]
    return " ".join(kept)


def _looks_like_a_file_request(name: str) -> bool:
    """«Пришли ответ целиком ещё раз» — это не про файл, а про сообщение."""
    query = _file_query(name)
    if not query:
        return True                       # «пришли мне этот файл» — спросим, какой
    first = query.split()[0].lower().strip(".,!?«»\"'")
    return first not in NOT_A_FILE


def _clean_up(path) -> None:
    """Записанный ответ на диске не залёживается: он уже ушёл в чат."""
    for candidate in ([Path(path), Path(path).with_suffix(".ogg")] if path else []):
        try:
            candidate.unlink()
        except OSError:
            pass


def _file_lines(paths, workdir) -> str:
    return "\n".join(texts.FILE_LINE.format(name=postman.relative(p, workdir))
                     for p in paths)
