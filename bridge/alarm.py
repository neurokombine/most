"""Будильник: расписание живёт внутри моста, а не в системных службах.

Почему своя таблица, а не `systemd` и не `cron`. Задача ставится словами
с телефона — «каждое утро в 7:30 собирай сводку по моему делу». Ни один юнит
так не заводится: там нужен терминал, права и точный синтаксис. Плюс всё
незаменимое остаётся в одном файле `most.db` — его и надо беречь.

Разбирает фразу сам мост, простыми правилами, **без нейросети**: время и день
недели — это не задача на понимание, и платить за них подпиской незачем.
Чего правила не разобрали, мост честно переспрашивает одной фразой с примером,
а не догадывается.

**Время — московское.** Часы машины не спрашиваются нигде: сервер у ученика
стоит в Амстердаме или во Франкфурте, а живёт он в Москве. Зона берётся из
настроек (`timezone`, по умолчанию `Europe/Moscow`) через `zoneinfo`; не
прочиталась — запасной путь всё равно московский, и мост говорит об этом вслух.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import changes, texts

try:                                       # на голой системе tzdata может не быть
    from zoneinfo import ZoneInfo
except ImportError:                        # pragma: no cover
    ZoneInfo = None

MOSCOW_NAME = "Europe/Moscow"
MOSCOW_FALLBACK = timezone(timedelta(hours=3))

# Мост был выключен дольше — догонять кучей не будем: шесть работ подряд
# в девять утра съедят подписку и завалят чат. Одна строка в журнал и в сводку.
MISSED_AFTER = 6 * 3600
LATE_GRACE = 300          # опоздание меньше пяти минут — обычный ход, молчим
SUMMARY_KEY = "summary_sent_on"       # маркер «сводка за эти сутки уже ушла»
DAY = 24 * 3600
RESULT_LIMIT = 500        # первые пятьсот знаков итога — в отчёт
FILES_IN_REPORT = 5
PROMPT_HEAD = 60

DAILY = "daily"
WEEKDAYS = "weekdays"
WEEKLY = "weekly"

# Сколько слов позволено до слов о расписании. Настоящая просьба начинается
# с них («каждое утро…»), а в «расскажи, что читают каждый день» они стоят
# глубже — и это уже обычная задача, а не расписание.
WORDS_BEFORE = 3

DAY_STEMS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень"]
DAY_PLURAL = ["понедельникам", "вторникам", "средам", "четвергам",
              "пятницам", "субботам", "воскресеньям"]
STEMS = "|".join(DAY_STEMS)

HINT_RE = re.compile(
    r"кажд\w+\s+(?:день|дня|сутки|утро|утром|вечер|вечером|ночь|ночью|недел\w+|"
    + STEMS + r"\w*)"
    r"|ежедневно|еженедельно"
    r"|по\s+будн\w+|в\s+будни|по\s+рабочим\s+дням"
    r"|по\s+утрам|по\s+вечерам"
    r"|раз\s+в\s+(?:день|сутки|недел\w+)"
    r"|по\s+(?:" + STEMS + r")\w*", re.IGNORECASE)

WEEKDAYS_RE = re.compile(r"по\s+будн\w+|в\s+будни|кажд\w+\s+будн\w+\s+д\w+|"
                         r"по\s+рабочим\s+дням", re.IGNORECASE)
WEEKLY_RE = re.compile(r"раз\s+в\s+недел\w*|еженедельно|кажд\w+\s+недел\w*", re.IGNORECASE)
NAMED_WEEKLY_RE = re.compile(r"(?:по|кажд\w+)\s+(?:" + STEMS + r")\w*", re.IGNORECASE)
DAILY_RE = re.compile(r"кажд\w*\s+(?:день|сутки|утро|утром|вечер|вечером|ночь|ночью)|"
                      r"ежедневно|по\s+утрам|по\s+вечерам|раз\s+в\s+(?:день|сутки)",
                      re.IGNORECASE)
# Предлог перед днём недели входит в найденное: иначе от «в понедельник»
# в задании останется висеть одинокое «в».
DAY_RE = re.compile(r"(?:(?:по|во|в|кажд\w+)\s+)?(?:"
                    + "|".join(f"(?P<d{i}>{stem}\\w*)" for i, stem in enumerate(DAY_STEMS))
                    + ")", re.IGNORECASE)

NUMBER_WORDS = {
    "час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "полдень": 12, "полночь": 0,
}
PART_OF_DAY = r"утра|утром|вечера|вечером|дня|ночи|днем"
TIME_EXACT_RE = re.compile(r"\bв\s+(\d{1,2})[:.](\d{2})\b")
TIME_DIGIT_RE = re.compile(r"\bв\s+(\d{1,2})(?:\s*час\w*)?(?:\s+(" + PART_OF_DAY + r"))?\b",
                           re.IGNORECASE)
TIME_WORD_RE = re.compile(r"\bв\s+(" + "|".join(NUMBER_WORDS) + r")(?:\s*час\w*)?"
                          r"(?:\s+(" + PART_OF_DAY + r"))?\b", re.IGNORECASE)

# Отдельные часы для «поменяй время задачи 2 на 8:00»: здесь предлог другой,
# а в разборе целой фразы «на 5 страниц» временем притворяться не должно.
NEW_TIME_RE = re.compile(r"(?:^|\s)(?:на|в|к)?\s*(\d{1,2})[:.](\d{2})(?:\s|$)")
NEW_HOUR_RE = re.compile(r"(?:^|\s)(?:на|в|к)\s*(\d{1,2})(?:\s*час\w*)?"
                         r"(?:\s+(" + PART_OF_DAY + r"))?(?:\s|$)", re.IGNORECASE)

_warned: set[str] = set()


# --- часовой пояс -----------------------------------------------------------

def zone(name: str | None = None):
    """Зона, в которой мост считает время. По умолчанию — московская.

    Часы машины не берём никогда: «в 7:30» для человека — это 7:30 в Москве,
    где бы ни стоял его сервер.
    """
    wanted = (name or MOSCOW_NAME).strip() or MOSCOW_NAME
    if ZoneInfo is not None:
        try:
            return ZoneInfo(wanted)
        except Exception:                               # noqa: BLE001
            if wanted != MOSCOW_NAME:
                _warn(texts.TIMEZONE_UNKNOWN.format(name=wanted, used=MOSCOW_NAME))
                try:
                    return ZoneInfo(MOSCOW_NAME)
                except Exception:                       # noqa: BLE001
                    pass
    _warn(texts.TIMEZONE_FALLBACK.format(used=MOSCOW_NAME))
    return MOSCOW_FALLBACK


def _warn(text: str) -> None:
    """Молчаливого ухода на зону машины быть не должно — о подмене говорим вслух."""
    if text in _warned:
        return
    _warned.add(text)
    print(f"мост: {text}", flush=True)


def now(config=None) -> datetime:
    return datetime.now(zone(getattr(config, "timezone", None)))


def to_iso(moment: datetime) -> str:
    """В базу время ложится в UTC — так его сравнивают строками, не гадая о зоне."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(raw: str, tz=None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz or zone())


def clock_face(hour: int, minute: int) -> str:
    return f"{hour}:{minute:02d}"


def stranger_name(row) -> str:
    """Как назвать постучавшегося: именем, а номером — только если имени нет.

    Своего номера в мессенджере человек не знает и знать не должен, а чужого
    тем более: «Екатерина Смирнова (Max)» говорит ему всё, «id 19520030» — ничего.
    """
    channel = texts.CHANNEL_NAMES.get(row["channel"], row["channel"])
    name = str(row["name"] or "").strip() if "name" in row.keys() else ""
    return f"{name} ({channel})" if name else f"id {row['user_id']} ({channel})"


def when_text(moment: datetime, tz=None) -> str:
    """Дата и время человеку — всегда с пометкой МСК, чтобы не гадать, чья зона."""
    return moment.astimezone(tz or zone()).strftime("%d.%m %H:%M МСК")


# --- само расписание --------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """Когда запускать: каждый день, по будням или в свой день недели."""

    kind: str
    hour: int
    minute: int
    weekday: int = -1          # 0 — понедельник; -1 — день недели не важен

    def to_stored(self) -> str:
        face = f"{self.hour:02d}:{self.minute:02d}"
        if self.kind == WEEKLY:
            return f"{WEEKLY} {self.weekday} {face}"
        return f"{self.kind} {face}"

    @classmethod
    def stored(cls, raw: str) -> "Spec | None":
        """Читает строку из базы. Мусор в строке не роняет мост, а просто не читается."""
        parts = str(raw or "").split()
        try:
            if len(parts) == 2 and parts[0] in (DAILY, WEEKDAYS):
                hour, minute = _hhmm(parts[1])
                return cls(parts[0], hour, minute)
            if len(parts) == 3 and parts[0] == WEEKLY:
                hour, minute = _hhmm(parts[2])
                weekday = int(parts[1])
                if 0 <= weekday <= 6:
                    return cls(WEEKLY, hour, minute, weekday)
        except (TypeError, ValueError):
            return None
        return None

    def human(self) -> str:
        face = clock_face(self.hour, self.minute)
        if self.kind == WEEKDAYS:
            return texts.SPEC_WEEKDAYS.format(time=face)
        if self.kind == WEEKLY:
            return texts.SPEC_WEEKLY.format(day=DAY_PLURAL[self.weekday], time=face)
        return texts.SPEC_DAILY.format(time=face)

    def suits(self, moment: datetime) -> bool:
        if self.kind == WEEKDAYS:
            return moment.weekday() < 5
        if self.kind == WEEKLY:
            return moment.weekday() == self.weekday
        return True


def _hhmm(raw: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in str(raw).split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(raw)
    return hour, minute


def next_run(spec: Spec, after: datetime, tz=None) -> datetime:
    """Ближайший запуск строго после `after`. Считается в московских сутках.

    Граница суток здесь московская, а не UTC: вечер на машине — это уже
    следующее утро в Москве, и «каждое утро в 7:30» не должно съехать на день.
    """
    tz = tz or zone()
    local = after.astimezone(tz)
    candidate = local.replace(hour=spec.hour, minute=spec.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    for _ in range(8):
        if spec.suits(candidate):
            return candidate
        candidate += timedelta(days=1)
    return candidate                      # сюда не доходим: неделя всегда закрывается


# --- разбор фразы словами ---------------------------------------------------

def looks_like_schedule(text: str) -> bool:
    """Это вообще про расписание? Отвечаем до разбора, чтобы было что переспросить."""
    return _hint(_flatten(text)) is not None


def parse(text: str) -> tuple[Spec | None, str]:
    """Фраза → («когда», «что делать»). Не разобрали — (None, "").

    Не разобрали — это не беда и не отказ: мост переспросит одной фразой
    с примером, и человек скажет то же самое, но со временем.
    """
    raw = (text or "").strip()
    low = _flatten(raw)
    if _hint(low) is None:
        return None, ""

    kind, weekday, spans = _kind_of(low)
    if kind is None:
        return None, ""

    clock = _time_of(low, evening=_evening(low))
    if clock is None:
        return None, ""
    hour, minute, time_span = clock
    spans.append(time_span)

    return Spec(kind, hour, minute, weekday), _without(raw, spans)


def parse_time(text: str) -> tuple[int, int] | None:
    """Только время: «на 8:00», «в 8 утра», «8:00». Не разобрали — None."""
    low = _flatten(text)
    exact = NEW_TIME_RE.search(low)
    if exact is not None:
        hour, minute = int(exact.group(1)), int(exact.group(2))
        return (hour, minute) if _sane(hour, minute) else None
    hour_only = NEW_HOUR_RE.search(low)
    if hour_only is not None:
        hour = _to_24(int(hour_only.group(1)), (hour_only.group(2) or "").lower(), False)
        return (hour, 0) if _sane(hour, 0) else None
    return None


def _flatten(text: str) -> str:
    """Для разбора — нижний регистр и «е» вместо «ё». Длина не меняется:
    места найденного те же, и вырезать их можно прямо из исходной фразы."""
    return (text or "").lower().replace("ё", "е")


def _hint(low: str):
    for found in HINT_RE.finditer(low):
        if len(low[:found.start()].split()) <= WORDS_BEFORE:
            return found
    return None


def _kind_of(low: str):
    """Каждый день · по будням · раз в неделю в свой день."""
    weekdays = WEEKDAYS_RE.search(low)
    if weekdays is not None:
        return WEEKDAYS, -1, [weekdays.span()]

    day = _day_of(low)
    weekly = WEEKLY_RE.search(low)
    named = NAMED_WEEKLY_RE.search(low)
    if day is not None and (weekly is not None or named is not None):
        spans = [day[1]]
        for found in (weekly, named):
            if found is not None:
                spans.append(found.span())
        return WEEKLY, day[0], spans

    daily = DAILY_RE.search(low)
    if daily is not None:
        return DAILY, -1, [daily.span()]
    return None, -1, []


def _day_of(low: str):
    found = DAY_RE.search(low)
    if found is None:
        return None
    for index in range(len(DAY_STEMS)):
        if found.group(f"d{index}"):
            return index, found.span()
    return None


def _evening(low: str) -> bool:
    """«Каждый вечер в 9» — это девять вечера, а не девять утра."""
    return bool(re.search(r"вечер|по\s+вечерам", low))


def _time_of(low: str, evening: bool = False):
    exact = TIME_EXACT_RE.search(low)
    if exact is not None:
        hour, minute = int(exact.group(1)), int(exact.group(2))
        return (hour, minute, exact.span()) if _sane(hour, minute) else None

    for pattern, table in ((TIME_DIGIT_RE, None), (TIME_WORD_RE, NUMBER_WORDS)):
        found = pattern.search(low)
        if found is None:
            continue
        raw = found.group(1)
        hour = table[raw] if table else int(raw)
        hour = _to_24(hour, (found.group(2) or "").lower(), evening)
        if _sane(hour, 0):
            return hour, 0, found.span()
    return None


def _to_24(hour: int, part: str, evening: bool) -> int:
    """Час суток. Сказали «вечера» — верим слову; не сказали — смотрим на «каждый вечер»."""
    if part.startswith("утр"):
        return 0 if hour == 12 else hour
    if part.startswith(("вечер", "дня", "днем")):
        return hour + 12 if hour < 12 else hour
    if part.startswith("ноч"):
        return 0 if hour == 12 else hour
    if evening and hour < 12:
        return hour + 12
    return hour


def _sane(hour: int, minute: int) -> bool:
    return 0 <= hour < 24 and 0 <= minute < 60


def _without(raw: str, spans) -> str:
    """Вырезает из фразы слова про «когда» — остаётся ровно задание."""
    letters = list(raw)
    for start, end in spans:
        for index in range(start, min(end, len(letters))):
            letters[index] = " "
    return " ".join("".join(letters).split()).strip(" ,;:.—–-")


def how_long(seconds) -> str:
    """Сколько длилась работа — словами, а не в секундах с точкой."""
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "сколько шла — не знаю"
    if seconds < 60:
        return f"{seconds} с"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} мин {rest} с" if rest else f"{minutes} мин"


def plural(count: int, forms: tuple[str, str, str]) -> str:
    """«1 раз», «2 раза», «5 раз» — чтобы мост не писал «5 раз(а)»."""
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return forms[0]
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return forms[1]
    return forms[2]


def money(amount: float) -> str:
    return f"{amount:.2f}".replace(".", ",") + " $"


def short(text: str, limit: int = PROMPT_HEAD) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


class Scheduler:
    """Будильник моста: смотрит на часы, ставит работы и обязательно отчитывается.

    Своей очереди у него нет: работа уходит в тот же `WorkPool`, что и разговор
    в чате. Иначе в одной папке окажутся две нейросети сразу, а слово «стоп»
    будет останавливать только одну из них.

    Отчёт после каждого прогона — не вежливость, а условие: тишина ответом
    не считается. Поэтому и уходит он во все настроенные мессенджеры разом
    (`Bridge.broadcast`), а не только в тот чат, где задачу однажды завели.
    """

    def __init__(self, config, store, pool, postbox=None, clock=None):
        self.config = config
        self.store = store
        self.pool = pool
        self.postbox = postbox
        self.tz = zone(getattr(config, "timezone", None))
        self.every = int(getattr(getattr(config, "schedule", None), "tick_sec", 30) or 30)
        self._clock = clock
        self._last_look = 0.0
        self.looks = 0                    # сколько раз смотрели на часы (для тестов)

    # --- часы ---------------------------------------------------------------

    def now(self) -> datetime:
        moment = self._clock() if self._clock is not None else datetime.now(self.tz)
        return moment.astimezone(self.tz)

    def projects_dir(self) -> Path:
        return Path(self.config.projects_dir)

    def workdir_of(self, row) -> Path:
        """Папка задачи — та, в которой её задали, даже если чат давно переключили."""
        project = row["project"]
        if not project:
            link = self.store.get_link_by_id(row["link_id"])
            project = link["project"] if link else None
        return self.projects_dir() / (project or "")

    # --- один взгляд на часы -------------------------------------------------

    def tick(self, now: datetime | None = None) -> int:
        """Заход будильника. Зовётся из главного цикла — не чаще, чем раз в `tick_sec`."""
        if now is None:
            if time.monotonic() - self._last_look < self.every:
                return 0
            self._last_look = time.monotonic()
            self.looks += 1
            now = self.now()
        else:
            now = now.astimezone(self.tz)
        return self.run_due(now) + self.maybe_summary(now)

    # --- поставить задачу ----------------------------------------------------

    def add(self, link, spec: Spec, prompt: str, project: str | None = None):
        project = project or link["project"]
        task_id = self.store.add_schedule(
            link_id=link["id"], spec=spec.to_stored(), prompt=prompt, project=project,
            next_run_at=to_iso(next_run(spec, self.now(), self.tz)))
        return self.store.get_schedule(task_id)

    # --- кому пора ------------------------------------------------------------

    def run_due(self, now: datetime) -> int:
        started = 0
        for row in self.store.list_schedule(only_enabled=True):
            spec = Spec.stored(row["spec"])
            if spec is None:
                # Строка в базе испорчена: молча запускать непонятно что нельзя.
                self.store.note("error", text=f"расписание задачи {row['id']} не читается")
                self.store.enable_schedule(row["id"], False)
                continue

            due = from_iso(row["next_run_at"], self.tz) if row["next_run_at"] else None
            if due is None:
                self.store.set_schedule_next(row["id"], to_iso(next_run(spec, now, self.tz)))
                continue
            if due > now:
                continue

            late = (now - due).total_seconds()
            if late > MISSED_AFTER:
                self._missed(row, spec, now)
                continue
            if self.launch(row, spec=spec, now=now) is None:
                continue                  # руки заняты — вернёмся через полминуты
            if late > LATE_GRACE:
                self.announce(texts.SCHEDULE_LATE.format(
                    number=row["id"], when=clock_face(spec.hour, spec.minute)))
            started += 1
        return started

    def launch(self, row, spec: Spec | None = None, now: datetime | None = None,
               move_next: bool = True):
        """Ставит работу расписания в общую очередь. None — места сейчас нет."""
        now = now or self.now()
        spec = spec or Spec.stored(row["spec"])
        link = self.store.get_link_by_id(row["link_id"])
        if link is None:
            self.store.note("error", text=f"задача {row['id']}: чат, где её завели, пропал")
            self.store.enable_schedule(row["id"], False)
            return None

        work = self.pool.submit(link=link, channel=link["channel"],
                                chat_id=link["chat_id"], thread_id=link["thread_id"],
                                prompt=row["prompt"], workdir=self.workdir_of(row),
                                resume=bool(link["session_started"]))
        if work is None:
            return None

        work.meta["schedule_id"] = row["id"]
        work.meta["schedule_prompt"] = row["prompt"]
        self.store.set_job_schedule(work.job_id, row["id"])
        self.store.mark_schedule_run(
            row["id"], status="running", at=to_iso(now),
            next_run_at=to_iso(next_run(spec, now, self.tz)) if (spec and move_next) else None)
        return work

    def _missed(self, row, spec: Spec, now: datetime) -> None:
        """Проспали больше шести часов: записываем и ждём следующего раза."""
        text = texts.SCHEDULE_MISSED.format(number=row["id"],
                                            when=clock_face(spec.hour, spec.minute))
        self.store.note("missed", text=text)
        self.store.mark_schedule_run(row["id"], status="missed", at=to_iso(now),
                                     next_run_at=to_iso(next_run(spec, now, self.tz)))

    # --- обязательный отчёт ---------------------------------------------------

    def report(self, work) -> str:
        """Что сказать про доделанную работу расписания. Зовёт главный цикл."""
        meta = work.meta or {}
        number = meta.get("schedule_id", "?")
        head = short(meta.get("schedule_prompt") or work.prompt)
        result = work.result

        if result is None:
            return texts.SCHEDULE_REPORT_FAILED.format(
                number=number, prompt=head, error=texts.WORK_EMPTY_ANSWER)
        if result.stopped:
            return texts.SCHEDULE_REPORT_STOPPED.format(number=number, prompt=head)
        if result.timed_out:
            return texts.SCHEDULE_REPORT_TIMEOUT.format(
                number=number, prompt=head,
                budget=how_long(getattr(self.pool.executor, "timeout", 900)))
        if not result.ok:
            return texts.SCHEDULE_REPORT_FAILED.format(
                number=number, prompt=head,
                error=short(result.error or "работа завершилась неудачно", 300))

        lines = [texts.SCHEDULE_REPORT_OK.format(
            number=number, prompt=head, how_long=self._how_long_of(work),
            result=short(result.text or texts.WORK_EMPTY_ANSWER, RESULT_LIMIT))]
        files = self._files_of(work)
        if files:
            lines.append(texts.SCHEDULE_REPORT_FILES.format(files=", ".join(files)))
        return "\n".join(lines)

    def _how_long_of(self, work) -> str:
        row = self.store.get_job(work.job_id)
        seconds = row["duration_sec"] if row is not None else None
        if seconds is None:
            seconds = getattr(work.result, "duration_sec", None)
        return how_long(seconds)

    def _files_of(self, work) -> list[str]:
        """Файлы смотрим сами, а не по словам нейросети: отчёт — это не результат."""
        try:
            found = changes.changed_files(work.workdir, since=work.started_wall,
                                          limit=FILES_IN_REPORT)
        except Exception:                               # noqa: BLE001
            return []
        return [name for name, _mtime in found]

    # --- ежедневная сводка ----------------------------------------------------

    def maybe_summary(self, now: datetime) -> int:
        settings = getattr(self.config, "schedule", None)
        if settings is not None and not getattr(settings, "summary", True):
            return 0

        hour, minute = _hhmm(getattr(settings, "summary_at", "08:00") or "08:00")
        today = now.date().isoformat()
        marker = self.store.get_setting(SUMMARY_KEY)
        if marker == today or (now.hour, now.minute) < (hour, minute):
            return 0
        if marker is None:
            # Первое утро после установки: суток за спиной ещё нет, считать нечего.
            self.store.set_setting(SUMMARY_KEY, today)
            return 0
        if not self.announce(self.summary_text(now)):
            return 0                     # сказать было некому — скажем, когда будет
        self.store.set_setting(SUMMARY_KEY, today)
        return 1

    def summary_text(self, now: datetime) -> str:
        since = to_iso(now - timedelta(seconds=DAY))
        lines = [texts.SUMMARY_HEADER.format(at=when_text(now, self.tz)), ""]
        lines += self._runs_lines(since) or [texts.SUMMARY_NOTHING]
        lines += self._missed_lines(since)

        spent, known = self.store.spent_since(since)
        lines.append("")
        lines.append(texts.SUMMARY_COST.format(cost=money(spent)) if known
                     else texts.SUMMARY_COST_UNKNOWN)

        # Чужой — это тот, кого нет в списке своих сейчас, а не тот, кого не
        # было вчера вечером. Живая приёмка 15.09: Натэла стучалась до того,
        # как её пустили, и утром попала в собственную сводку как чужая.
        strangers = [row for row in self.store.strangers_since(since)
                     if not self.store.is_allowed(row["channel"], row["user_id"])]
        if strangers:
            who = ", ".join(sorted({stranger_name(row) for row in strangers}))
            lines.append(texts.SUMMARY_STRANGERS.format(
                count=len(strangers), times=plural(len(strangers), ("раз", "раза", "раз")),
                who=who))
        else:
            lines.append(texts.SUMMARY_NO_STRANGERS)
        return "\n".join(lines)

    def night_text(self, now: datetime | None = None) -> str:
        """«Покажи, что ты запускала ночью и чем закончилось» — дверь, а не отчёт."""
        now = now or self.now()
        since = to_iso(now - timedelta(seconds=DAY))
        runs = self._runs_lines(since)
        missed = self._missed_lines(since)
        if not runs and not missed:
            return texts.NIGHT_EMPTY
        return "\n".join([texts.NIGHT_HEADER] + runs + missed)

    def _runs_lines(self, since: str) -> list[str]:
        lines = []
        for row in self.store.jobs_since(since, only_scheduled=True):
            when = from_iso(row["started_at"], self.tz)
            lines.append(texts.NIGHT_LINE.format(
                at=when.strftime("%d.%m %H:%M") if when else row["started_at"],
                number=row["schedule_id"],
                outcome=texts.JOB_OUTCOME.get(row["state"], row["state"]),
                how_long=how_long(row["duration_sec"]),
                prompt=short(row["prompt_head"] or "")))
        return lines

    def _missed_lines(self, since: str) -> list[str]:
        return [texts.NIGHT_MISSED_LINE.format(text=row["text"])
                for row in self.store.journal_since("missed", since)]

    # --- сказать во все каналы -------------------------------------------------

    def announce(self, text: str, aloud: bool = True) -> int:
        if not text or self.postbox is None:
            return 0
        try:
            return int(self.postbox.broadcast(text, aloud=aloud) or 0)
        except Exception as exc:                        # noqa: BLE001
            self.store.note("error", text=f"не смог отчитаться о расписании: {exc}"[:200])
            return 0
