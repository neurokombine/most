"""Будильник: кто запускается, когда мост проспал, и обязательный отчёт.

Сети нет, настоящего `claude` нет, часы — свои: время передаётся в `tick`
руками, чтобы утро можно было проверить, не дожидаясь утра.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bridge import alarm
from bridge.alarm import Scheduler, Spec
from bridge.executor import FakeExecutor
from bridge.works import WorkPool

UTC = timezone.utc
MSK = alarm.zone()


def msk(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


class Postbox:
    """Мост как почтовый ящик: записываем, что он сказал бы во все каналы."""

    def __init__(self, delivered=2):
        self.said = []
        self.delivered = delivered

    def broadcast(self, text, file=None, aloud=False):
        self.said.append(text)
        return self.delivered


@pytest.fixture()
def pool(store):
    return WorkPool(executor=FakeExecutor(text="Сводка на утро: цены выросли на 3%."),
                    store=store, max_parallel=1)


@pytest.fixture()
def postbox():
    return Postbox()


@pytest.fixture()
def alarm_clock(config, store, pool, postbox):
    return Scheduler(config=config, store=store, pool=pool, postbox=postbox)


@pytest.fixture()
def link(store):
    return store.upsert_link("telegram", 100, 0, project="buhgalter", session_id="s-1")


def a_task(alarm_clock, link, spec="daily 07:30", prompt="собирай сводку по моему делу",
           project="buhgalter", when=None):
    row = alarm_clock.add(link, Spec.stored(spec), prompt, project=project)
    if when is not None:
        alarm_clock.store.set_schedule_next(row["id"], alarm.to_iso(when))
    return alarm_clock.store.get_schedule(row["id"])


# --- запуск по времени ------------------------------------------------------

def test_a_task_waits_for_its_hour(alarm_clock, link, pool):
    a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    assert alarm_clock.tick(now=msk(2026, 9, 16, 7, 0)) == 0
    assert pool.running() == 0


def test_a_task_runs_when_its_hour_comes(alarm_clock, link, pool, store):
    task = a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    assert alarm_clock.tick(now=msk(2026, 9, 16, 7, 30, )) == 1

    pool.wait_idle(timeout=5)
    work = pool.collect()[0]
    assert work.meta["schedule_id"] == task["id"]
    assert store.get_job(work.job_id)["schedule_id"] == task["id"]
    # Следующий запуск переставлен сразу: проспать второй раз в те же сутки нельзя.
    assert alarm.from_iso(store.get_schedule(task["id"])["next_run_at"]) == \
        msk(2026, 9, 17, 7, 30)


def test_a_task_runs_in_the_folder_where_it_was_set(alarm_clock, link, pool, store,
                                                    projects_dir):
    """Связку потом переключили на другую папку — задача осталась в своей."""
    task = a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    store.set_link_project("telegram", 100, 0, "analitika")

    alarm_clock.tick(now=msk(2026, 9, 16, 7, 30))
    pool.wait_idle(timeout=5)
    assert pool.collect()[0].workdir == projects_dir / "buhgalter"
    assert store.get_schedule(task["id"])["project"] == "buhgalter"


def test_a_switched_off_task_stays_quiet(alarm_clock, link, pool, store):
    task = a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    store.enable_schedule(task["id"], False)
    assert alarm_clock.tick(now=msk(2026, 9, 16, 9, 0)) == 0
    assert pool.running() == 0


def test_busy_hands_postpone_the_run_instead_of_losing_it(config, store, postbox, link):
    """Мост занят разговором — задача подождёт полминуты, а не пропадёт."""
    slow = WorkPool(executor=FakeExecutor(text="…", delay=5), store=store, max_parallel=1)
    clock = Scheduler(config=config, store=store, pool=slow, postbox=postbox)
    task = a_task(clock, link, when=msk(2026, 9, 16, 7, 30))
    slow.submit(link=store.get_link_by_id(link["id"]), channel="telegram", chat_id=100,
                prompt="ручная задача", workdir="/tmp", thread_id=0)

    assert clock.tick(now=msk(2026, 9, 16, 7, 30)) == 0
    assert alarm.from_iso(store.get_schedule(task["id"])["next_run_at"]) == \
        msk(2026, 9, 16, 7, 30)
    slow.stop_all()
    slow.wait_idle(timeout=5)


# --- мост был выключен ------------------------------------------------------

def test_a_short_miss_is_run_once_and_said_out_loud(alarm_clock, link, pool, postbox):
    a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    assert alarm_clock.tick(now=msk(2026, 9, 16, 9, 30)) == 1      # опоздание два часа
    assert any("7:30" in said and "сейчас" in said for said in postbox.said)
    pool.wait_idle(timeout=5)


def test_a_long_miss_is_only_written_down(alarm_clock, link, pool, postbox, store):
    task = a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    assert alarm_clock.tick(now=msk(2026, 9, 16, 21, 0)) == 0      # опоздание больше шести часов
    assert pool.running() == 0
    assert postbox.said == []                                      # среди ночи не будим
    written = store.journal_since("missed", "2026-01-01T00:00:00+00:00")
    assert len(written) == 1 and "7:30" in written[0]["text"]
    assert store.get_schedule(task["id"])["last_status"] == "missed"
    assert alarm.from_iso(store.get_schedule(task["id"])["next_run_at"]) == \
        msk(2026, 9, 17, 7, 30)


def test_a_missed_run_shows_up_in_the_next_summary(alarm_clock, link, store):
    a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    alarm_clock.tick(now=msk(2026, 9, 16, 21, 0))
    summary = alarm_clock.summary_text(alarm_clock.now())
    assert "пропустила" in summary.lower()


# --- обязательный отчёт -----------------------------------------------------

def test_report_says_how_long_it_took_and_what_came_out(alarm_clock, link, pool,
                                                        projects_dir):
    task = a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    alarm_clock.tick(now=msk(2026, 9, 16, 7, 30))
    pool.wait_idle(timeout=5)
    (projects_dir / "buhgalter" / "svodka.md").write_text("готово", encoding="utf-8")

    text = alarm_clock.report(pool.collect()[0])
    assert f"Задача {task['id']}" in text
    assert "цены выросли" in text
    assert "svodka.md" in text


def test_report_of_a_broken_run_is_human(config, store, postbox, link, projects_dir):
    pool = WorkPool(executor=FakeExecutor(text="папки нет", ok=False, exit_code=1),
                    store=store, max_parallel=1)
    clock = Scheduler(config=config, store=store, pool=pool, postbox=postbox)
    task = a_task(clock, link, when=msk(2026, 9, 16, 7, 30))
    clock.tick(now=msk(2026, 9, 16, 7, 30))
    pool.wait_idle(timeout=5)

    text = clock.report(pool.collect()[0])
    assert "не вышло" in text.lower() and f"Задача {task['id']}" in text


def test_report_of_a_stopped_run_does_not_call_it_a_failure(config, store, postbox, link):
    pool = WorkPool(executor=FakeExecutor(text="…", delay=5, partial="успела начать"),
                    store=store, max_parallel=1)
    clock = Scheduler(config=config, store=store, pool=pool, postbox=postbox)
    a_task(clock, link, when=msk(2026, 9, 16, 7, 30))
    clock.tick(now=msk(2026, 9, 16, 7, 30))

    stopped = pool.stop_in_dir(clock.workdir_of(store.get_schedule(1)))
    assert len(stopped) == 1
    pool.wait_idle(timeout=5)
    text = clock.report(pool.collect()[0])
    assert "остановила" in text.lower()


# --- ежедневная сводка ------------------------------------------------------

def test_the_first_morning_after_install_is_quiet(alarm_clock, postbox, store):
    """Сутки считать не с чего: мост стоит первый день."""
    assert alarm_clock.tick(now=msk(2026, 9, 16, 8, 0)) == 0
    assert postbox.said == []
    assert store.get_setting(alarm.SUMMARY_KEY) == "2026-09-16"


def test_the_summary_comes_the_next_morning(alarm_clock, postbox, store):
    store.set_setting(alarm.SUMMARY_KEY, "2026-09-15")
    assert alarm_clock.tick(now=msk(2026, 9, 16, 8, 0)) == 1
    assert "Сводка моста" in postbox.said[0]
    assert store.get_setting(alarm.SUMMARY_KEY) == "2026-09-16"


def test_the_summary_is_not_repeated_after_a_restart(alarm_clock, postbox, store):
    store.set_setting(alarm.SUMMARY_KEY, "2026-09-15")
    alarm_clock.tick(now=msk(2026, 9, 16, 8, 0))
    alarm_clock.tick(now=msk(2026, 9, 16, 8, 1))
    alarm_clock.tick(now=msk(2026, 9, 16, 23, 0))
    assert len(postbox.said) == 1


def test_nothing_is_sent_when_no_messenger_is_set_up(config, store, pool):
    """Каналов нет — сводка не уходит и маркер не ставится: скажем, когда будет кому."""
    silent = Postbox(delivered=0)
    clock = Scheduler(config=config, store=store, pool=pool, postbox=silent)
    store.set_setting(alarm.SUMMARY_KEY, "2026-09-15")
    assert clock.tick(now=msk(2026, 9, 16, 8, 0)) == 0
    assert store.get_setting(alarm.SUMMARY_KEY) == "2026-09-15"


def test_the_summary_can_be_switched_off(config, store, pool, postbox):
    config.schedule.summary = False
    clock = Scheduler(config=config, store=store, pool=pool, postbox=postbox)
    store.set_setting(alarm.SUMMARY_KEY, "2026-09-15")
    assert clock.tick(now=msk(2026, 9, 16, 8, 0)) == 0
    assert postbox.said == []


def test_the_summary_counts_money_runs_and_strangers(alarm_clock, link, store):
    task = a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    job = store.start_job(link["id"], "telegram", 100, "s-1", "собирай сводку", "")
    store.set_job_schedule(job, task["id"])
    store.finish_job(job, state="done", duration_sec=63.0, cost_usd=0.21,
                     result_head="цены выросли")
    store.note_stranger("telegram", 7, 555, "кто ты")

    summary = alarm_clock.summary_text(msk(2026, 9, 16, 8, 0))
    assert "0,21" in summary
    assert "чуж" in summary.lower() and "555" in summary
    assert "готово" in summary


def test_the_summary_is_honest_when_the_price_is_unknown(alarm_clock, link, store):
    task = a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    job = store.start_job(link["id"], "telegram", 100, "s-1", "сводка", "")
    store.set_job_schedule(job, task["id"])
    store.finish_job(job, state="done", duration_sec=5.0)

    summary = alarm_clock.summary_text(msk(2026, 9, 16, 8, 0))
    assert "не назвала" in summary


def test_an_empty_day_is_still_reported(alarm_clock):
    summary = alarm_clock.summary_text(msk(2026, 9, 16, 8, 0))
    assert "ничего не запускал" in summary.lower()


# --- «покажи, что ты запускала ночью» ---------------------------------------

def test_the_night_log_shows_scheduled_runs_only(alarm_clock, link, store):
    task = a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    by_hand = store.start_job(link["id"], "telegram", 100, "s-1", "посчитай выручку", "")
    store.finish_job(by_hand, state="done", duration_sec=4.0)
    by_alarm = store.start_job(link["id"], "telegram", 100, "s-1", "собирай сводку", "")
    store.set_job_schedule(by_alarm, task["id"])
    store.finish_job(by_alarm, state="done", duration_sec=185.0)

    text = alarm_clock.night_text(msk(2026, 9, 16, 8, 0))
    assert "собирай сводку" in text
    assert "посчитай выручку" not in text
    assert "3 мин 5 с" in text


def test_the_night_log_is_honest_when_nothing_ran(alarm_clock):
    assert "ничего" in alarm_clock.night_text(msk(2026, 9, 16, 8, 0)).lower()


def test_the_night_log_names_a_miss(alarm_clock, link, store):
    """Журнал пишется настоящими часами, поэтому и смотрим на него настоящими."""
    a_task(alarm_clock, link, when=msk(2026, 9, 16, 7, 30))
    alarm_clock.tick(now=msk(2026, 9, 16, 21, 0))
    assert "пропустила" in alarm_clock.night_text().lower()


# --- часы моста -------------------------------------------------------------

def test_the_bridge_looks_at_the_clock_not_more_often_than_asked(config, store, pool,
                                                                 postbox):
    config.schedule.tick_sec = 3600
    clock = Scheduler(config=config, store=store, pool=pool, postbox=postbox)
    assert clock.tick() == 0          # первый заход часы читает
    assert clock.tick() == 0          # второй — уже нет, и это видно по счётчику
    assert clock.looks == 1


def test_the_clock_of_the_bridge_is_moscow_even_when_the_machine_is_not(config, store,
                                                                       pool, postbox):
    clock = Scheduler(config=config, store=store, pool=pool, postbox=postbox,
                      clock=lambda: datetime(2026, 9, 15, 21, 30, tzinfo=UTC))
    assert clock.now() == msk(2026, 9, 16, 0, 30)
    assert clock.now().utcoffset() == timedelta(hours=3)


# --- кто в сводке чужой (живая приёмка 15.09) -------------------------------

def test_the_summary_forgets_those_who_are_now_allowed(alarm_clock, store):
    """Натэла стучалась до того, как её пустили, — и попала в сводку как чужая."""
    store.note_stranger("telegram", 457475813, 457475813, "привет",
                        name="Натэла Зубченко")
    store.allow("telegram", 457475813, note="Натэла Зубченко")

    summary = alarm_clock.summary_text(msk(2026, 9, 16, 8, 0))
    assert "чужие не писали" in summary.lower()
    assert "457475813" not in summary


def test_the_summary_calls_strangers_by_name(alarm_clock, store):
    """Номер человеку ничего не говорит, имя говорит всё."""
    store.note_stranger("max", -1, 19520030, "а когда третий модуль",
                        name="Екатерина Смирнова")
    summary = alarm_clock.summary_text(msk(2026, 9, 16, 8, 0))
    assert "Екатерина Смирнова" in summary
    assert "19520030" not in summary


def test_a_nameless_stranger_is_still_shown_by_number(alarm_clock, store):
    store.note_stranger("telegram", 7, 555, "кто ты")
    summary = alarm_clock.summary_text(msk(2026, 9, 16, 8, 0))
    assert "555" in summary
