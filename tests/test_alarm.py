"""Разбор расписания словами и расчёт следующего запуска.

Нейросети здесь нет и быть не должно: «каждое утро в 7:30» — это правило,
а не задача на понимание. Разбирает мост сам, а чего не разобрал — переспрашивает.
Все тесты держат машину в UTC, а задачу — в московском времени: у ученика
сервер стоит где угодно, а живёт он в Москве.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bridge import alarm

UTC = timezone.utc


def msk(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=alarm.zone())


# --- что мост понимает ------------------------------------------------------

@pytest.mark.parametrize("phrase, kind, hour, minute, weekday", [
    ("каждое утро в 7:30 собирай сводку по моему делу", "daily", 7, 30, -1),
    ("каждый день в 21:00 проверяй, открывается ли сайт", "daily", 21, 0, -1),
    ("каждый день в 9:05 смотри почту", "daily", 9, 5, -1),
    ("по будням в 9:00 собирай новости рынка", "weekdays", 9, 0, -1),
    ("в будни в 18:30 присылай итог дня", "weekdays", 18, 30, -1),
    ("раз в неделю в понедельник в 8:00 делай разбор недели", "weekly", 8, 0, 0),
    ("каждый понедельник в 8:00 пиши план на неделю", "weekly", 8, 0, 0),
    ("по вторникам в 10:00 проверяй остатки", "weekly", 10, 0, 1),
    ("каждую среду в 12:30 считай выручку", "weekly", 12, 30, 2),
    ("по четвергам в 7:00 присылай погоду", "weekly", 7, 0, 3),
    ("каждую пятницу в 17:00 собирай итоги", "weekly", 17, 0, 4),
    ("по субботам в 11:00 проверяй заказы", "weekly", 11, 0, 5),
    ("по воскресеньям в 10:00 пиши, что было за неделю", "weekly", 10, 0, 6),
    ("каждое утро в семь собери сводку", "daily", 7, 0, -1),
    ("каждый день в 7 утра присылай прогноз", "daily", 7, 0, -1),
    ("каждый день в 9 вечера пиши итог", "daily", 21, 0, -1),
    ("каждый вечер в 9 пиши итог дня", "daily", 21, 0, -1),
    ("каждый день в 8 часов утра проверяй заявки", "daily", 8, 0, -1),
    ("ежедневно в 06:15 проверяй курс", "daily", 6, 15, -1),
    ("каждое утро в 7.30 собирай сводку", "daily", 7, 30, -1),
    ("каждую ночь в 3:00 делай резервную копию", "daily", 3, 0, -1),
    ("каждый день в час дня напоминай про обед", "daily", 13, 0, -1),
])
def test_phrases_the_bridge_understands_on_its_own(phrase, kind, hour, minute, weekday):
    spec, prompt = alarm.parse(phrase)
    assert spec is not None, phrase
    assert (spec.kind, spec.hour, spec.minute, spec.weekday) == (kind, hour, minute, weekday)
    assert prompt and "кажд" not in prompt.lower() and ":" not in prompt.split()[0]


# --- чего мост не понимает и честно переспрашивает ---------------------------

@pytest.mark.parametrize("phrase", [
    "каждое утро собирай сводку",                      # когда именно — не сказано
    "каждый день в половине восьмого собирай сводку",   # время словами не разбираю
    "раз в неделю собирай отчёт",                       # в какой день — не сказано
    "по будням собирай новости",
    "каждый день в 25:00 проверяй почту",               # такого часа не бывает
    "каждый день в 7:99 проверяй почту",
    "каждое утро в 7:30",                               # время есть, дела нет
])
def test_phrases_the_bridge_asks_again_about(phrase):
    assert alarm.looks_like_schedule(phrase) is True
    spec, prompt = alarm.parse(phrase)
    assert spec is None or not prompt


@pytest.mark.parametrize("phrase", [
    "посчитай выручку за август",
    "пришли мне отчёт",
    "что ты делала вчера",
    "сделай это в понедельник",          # разовая просьба, а не расписание
    "расскажи, что нового в мире каждый день читают в новостях",  # маркер далеко от начала
])
def test_ordinary_tasks_are_not_mistaken_for_a_schedule(phrase):
    assert alarm.looks_like_schedule(phrase) is False


# --- что остаётся заданием --------------------------------------------------

def test_task_text_keeps_everything_except_the_when():
    spec, prompt = alarm.parse(
        "каждое утро в 7:30 собирай сводку по моему делу: цены на сырьё и новости конкурентов")
    assert spec.kind == "daily"
    assert prompt == "собирай сводку по моему делу: цены на сырьё и новости конкурентов"


def test_task_text_survives_when_the_time_stands_at_the_end():
    spec, prompt = alarm.parse("каждый день собирай сводку по рынку в 7:30")
    assert (spec.hour, spec.minute) == (7, 30)
    assert prompt == "собирай сводку по рынку"


def test_yo_letter_does_not_break_the_parsing():
    spec, prompt = alarm.parse("каждый день в 7:30 счётчик проверяй")
    assert spec is not None and "счётчик" in prompt


# --- как расписание хранится и читается обратно -----------------------------

@pytest.mark.parametrize("stored", ["daily 07:30", "weekdays 09:00", "weekly 0 08:00"])
def test_stored_spec_reads_back_the_same(stored):
    assert alarm.Spec.stored(stored).to_stored() == stored


def test_broken_row_in_the_base_does_not_bring_the_bridge_down():
    assert alarm.Spec.stored("каждое утро") is None
    assert alarm.Spec.stored("") is None
    assert alarm.Spec.stored("daily 99:99") is None


def test_spec_says_when_it_runs_in_human_words():
    assert alarm.Spec.stored("daily 07:30").human() == "каждый день в 7:30"
    assert alarm.Spec.stored("weekdays 09:00").human() == "по будням в 9:00"
    assert alarm.Spec.stored("weekly 0 08:00").human() == "по понедельникам в 8:00"


# --- следующий запуск: границы суток и чужая зона машины --------------------

def test_next_run_is_tomorrow_when_today_is_already_past():
    spec = alarm.Spec.stored("daily 07:30")
    # На машине — вечер 15 сентября по UTC, а в Москве уже 16-е, половина
    # первого ночи. Правильный ответ — утро 16-го, а не 17-го.
    now = datetime(2026, 9, 15, 21, 30, tzinfo=UTC)
    assert alarm.next_run(spec, now) == msk(2026, 9, 16, 7, 30)


def test_next_run_crosses_midnight_in_moscow_not_in_utc():
    spec = alarm.Spec.stored("daily 01:00")
    now = datetime(2026, 9, 15, 23, 30, tzinfo=UTC)        # в Москве уже 16-е, 02:30
    assert alarm.next_run(spec, now) == msk(2026, 9, 17, 1, 0)


def test_next_run_at_the_very_minute_is_the_next_day():
    spec = alarm.Spec.stored("daily 07:30")
    assert alarm.next_run(spec, msk(2026, 9, 15, 7, 30)) == msk(2026, 9, 16, 7, 30)


def test_weekdays_skip_saturday_and_sunday():
    spec = alarm.Spec.stored("weekdays 09:00")
    friday_evening = msk(2026, 9, 18, 20, 0)               # пятница
    assert friday_evening.weekday() == 4
    assert alarm.next_run(spec, friday_evening) == msk(2026, 9, 21, 9, 0)   # понедельник


def test_weekly_waits_for_its_own_day():
    spec = alarm.Spec.stored("weekly 0 08:00")             # по понедельникам
    assert alarm.next_run(spec, msk(2026, 9, 15, 9, 0)) == msk(2026, 9, 21, 8, 0)


def test_next_run_is_stored_in_utc_and_read_back_to_moscow():
    spec = alarm.Spec.stored("daily 07:30")
    when = alarm.next_run(spec, datetime(2026, 9, 15, 21, 30, tzinfo=UTC))
    stored = alarm.to_iso(when)
    assert stored == "2026-09-16T04:30:00+00:00"
    assert alarm.from_iso(stored) == when


def test_moscow_zone_is_three_hours_ahead_of_utc():
    """Пояс читается через zoneinfo; не прочитался — запасной путь, но всё равно МСК."""
    offset = alarm.zone().utcoffset(datetime(2026, 9, 15, 12, tzinfo=UTC))
    assert offset == timedelta(hours=3)


def test_unknown_zone_falls_back_to_moscow_and_says_so(capsys):
    zone = alarm.zone("Луна/Море_Спокойствия")
    assert zone.utcoffset(datetime(2026, 9, 15, 12, tzinfo=UTC)) == timedelta(hours=3)
    assert "Europe/Moscow" in capsys.readouterr().out
