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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import texts

try:                                       # на голой системе tzdata может не быть
    from zoneinfo import ZoneInfo
except ImportError:                        # pragma: no cover
    ZoneInfo = None

MOSCOW_NAME = "Europe/Moscow"
MOSCOW_FALLBACK = timezone(timedelta(hours=3))

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
