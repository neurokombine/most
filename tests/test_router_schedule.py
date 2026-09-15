"""Расписание словами в чате: поставить, посмотреть, перенести, убрать, запустить.

Плюс два места, где этап 5 стыкуется с остальным мостом: «стоп» останавливает
и работу расписания в той же папке, а отчёт о прогоне уходит во все каналы.
"""
import pytest

from bridge import alarm
from bridge.daemon import Bridge
from bridge.executor import FakeExecutor
from bridge.receivers.base import Incoming
from bridge.router import Router


@pytest.fixture()
def router(config, store):
    store.sync_allowlist("telegram", config.telegram.allowlist)
    store.sync_allowlist("max", config.max.allowlist)
    r = Router(config=config, store=store, executor=FakeExecutor(text="Сводка готова."))
    yield r
    r.pool.stop_all()
    r.pool.wait_idle()


def tg(text, user_id=111, chat_id=500):
    return Incoming(channel="telegram", chat_id=chat_id, user_id=user_id,
                    text=text, thread_id=0, raw={})


def said(router, text):
    return "\n".join(router.handle(tg(text)))


# --- поставить --------------------------------------------------------------

def test_a_phrase_with_a_time_becomes_a_task_on_the_schedule(router, store):
    answer = said(router, "каждое утро в 7:30 собирай сводку по моему делу")
    assert "расписание" in answer.lower()
    rows = store.list_schedule()
    assert len(rows) == 1
    assert rows[0]["spec"] == "daily 07:30"
    assert rows[0]["prompt"] == "собирай сводку по моему делу"
    assert rows[0]["project"] == "analitika"      # папка чата по умолчанию
    assert rows[0]["next_run_at"]
    # Задача поставлена, а не выполнена прямо сейчас.
    assert router.pool.running() == 0


def test_the_task_belongs_to_the_folder_of_the_chat_where_it_was_set(router, store):
    said(router, "работаем с buhgalter")
    said(router, "по будням в 9:00 проверяй, открывается ли сайт")
    assert store.list_schedule()[0]["project"] == "buhgalter"


def test_an_unclear_schedule_phrase_is_asked_again_with_an_example(router, store):
    answer = said(router, "каждое утро собирай сводку")
    assert "7:30" in answer                      # пример прямо в переспросе
    assert store.list_schedule() == []
    assert router.pool.running() == 0            # и заданием это не стало


def test_a_phrase_without_a_task_is_asked_again_too(router, store):
    assert "дело" in said(router, "каждое утро в 7:30").lower()
    assert store.list_schedule() == []


def test_an_ordinary_task_is_still_an_ordinary_task(router, store):
    assert "работу" in said(router, "посчитай выручку за август")
    assert store.list_schedule() == []
    router.pool.wait_idle()


# --- посмотреть -------------------------------------------------------------

def test_empty_schedule_answers_with_an_example(router):
    assert "7:30" in said(router, "покажи расписание")


def test_the_schedule_list_shows_number_time_and_task(router):
    said(router, "каждое утро в 7:30 собирай сводку по моему делу")
    answer = said(router, "покажи расписание")
    assert "задача 1" in answer
    assert "каждый день в 7:30" in answer
    assert "собирай сводку" in answer
    assert "МСК" in answer


def test_the_question_from_the_lesson_shows_the_schedule(router):
    """«Покажи, какое задание стоит на расписании сейчас» — дверь из урока Б.7."""
    said(router, "каждое утро в 7:30 собирай сводку по моему делу")
    assert "задача 1" in said(router, "покажи, какое задание стоит на расписании сейчас")


def test_a_switched_off_task_is_shown_as_switched_off(router, store):
    said(router, "каждое утро в 7:30 собирай сводку")
    said(router, "выключи задачу 1")
    assert "выключена" in said(router, "покажи расписание")
    assert store.get_schedule(1)["enabled"] == 0


# --- поменять и убрать ------------------------------------------------------

def test_the_time_of_a_task_is_changed_by_words(router, store):
    said(router, "каждое утро в 7:30 собирай сводку")
    answer = said(router, "поменяй время задачи 1 на 8:00")
    assert "8:00" in answer
    assert store.get_schedule(1)["spec"] == "daily 08:00"


def test_a_nonsense_new_time_does_not_break_the_task(router, store):
    said(router, "каждое утро в 7:30 собирай сводку")
    assert "поменяй время" in said(router, "поменяй время задачи 1 на попозже")
    assert store.get_schedule(1)["spec"] == "daily 07:30"


def test_a_task_is_removed_by_words(router, store):
    said(router, "каждое утро в 7:30 собирай сводку")
    assert "убрала" in said(router, "убери задачу 1").lower()
    assert store.list_schedule() == []


def test_a_task_that_does_not_exist_is_answered_not_ignored(router):
    assert "нет" in said(router, "убери задачу 7").lower()


def test_a_switched_off_task_can_be_switched_on_again(router, store):
    said(router, "каждое утро в 7:30 собирай сводку")
    said(router, "выключи задачу 1")
    assert "включила" in said(router, "включи задачу 1").lower()
    assert store.get_schedule(1)["enabled"] == 1


# --- запустить сейчас -------------------------------------------------------

def test_a_task_can_be_run_right_now_without_moving_the_schedule(router, store):
    said(router, "каждое утро в 7:30 собирай сводку по моему делу")
    was = store.get_schedule(1)["next_run_at"]

    assert "сейчас" in said(router, "запусти задачу 1 сейчас")
    router.pool.wait_idle()
    work = router.pool.collect()[0]
    assert work.meta["schedule_id"] == 1
    assert store.get_schedule(1)["next_run_at"] == was


def test_running_a_task_while_busy_is_answered_honestly(config, store):
    store.sync_allowlist("telegram", [111])
    slow = Router(config=config, store=store,
                  executor=FakeExecutor(text="…", delay=5, partial="успела начать"))
    said(slow, "каждое утро в 7:30 собирай сводку")
    slow.handle(tg("посчитай выручку"))                   # руки заняты надолго

    assert "работаю" in said(slow, "запусти задачу 1 сейчас").lower()
    slow.pool.stop_all()
    slow.pool.wait_idle(timeout=5)


# --- «покажи, что ты запускала ночью» ---------------------------------------

def test_the_night_question_shows_scheduled_runs(router, store):
    said(router, "каждое утро в 7:30 собирай сводку по моему делу")
    said(router, "запусти задачу 1 сейчас")
    router.pool.wait_idle()
    router.pool.collect()

    answer = said(router, "покажи, что ты запускала ночью и чем закончилось")
    assert "собирай сводку" in answer
    assert "готово" in answer


def test_the_night_question_on_an_empty_journal_is_answered(router):
    assert "ничего" in said(router, "покажи, что ты запускала ночью").lower()


# --- «стоп» ------------------------------------------------------------------

def test_stop_kills_the_scheduled_work_in_the_same_folder(config, store):
    """Находка этапа 2: «стоп» гасил только свою связку, а ночная работа жила."""
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    config.parallel = 2
    router = Router(config=config, store=store,
                    executor=FakeExecutor(text="…", delay=5, partial="успела начать"))
    # Задача расписания заведена в чате Max, а «стоп» скажут из телеграма —
    # папка у них одна и та же.
    router.handle(Incoming(channel="max", chat_id=900, user_id=222, thread_id=0,
                           text="каждое утро в 7:30 собирай сводку", raw={}))
    router.alarm.launch(store.get_schedule(1))

    answer = "\n".join(router.handle(tg("стоп")))
    assert "задача 1" in answer.lower()
    router.pool.wait_idle(timeout=5)
    assert router.pool.running() == 0


def test_stop_without_anything_running_still_answers(router):
    assert "нечего" in said(router, "стоп").lower()


def test_stop_names_both_works_it_stopped(config, store):
    """Две работы в одной папке бывают только из двух чатов: в одном очередь
    держит ровно одну. Значит, «стоп» должен назвать обе."""
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    config.parallel = 2
    router = Router(config=config, store=store,
                    executor=FakeExecutor(text="…", delay=5, partial="успела начать"))
    router.handle(Incoming(channel="max", chat_id=900, user_id=222, thread_id=0,
                           text="каждое утро в 7:30 собирай сводку", raw={}))
    router.alarm.launch(store.get_schedule(1))       # ночная работа из чата Max
    router.handle(tg("посчитай выручку"))            # своя работа в телеграме

    answer = "\n".join(router.handle(tg("стоп"))).lower()
    assert "задача 1" in answer
    assert "посчитай выручку" in answer
    router.pool.wait_idle(timeout=5)


# --- отчёт уходит во все каналы ---------------------------------------------

class FakeReceiver:
    def __init__(self, channel, limit=4096):
        self.channel = channel
        self.limit = limit
        self.sent = []

    def poll_once(self):
        return []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))


def bridge_with(config, store, receivers):
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    return Bridge(config=config, store=store,
                  executor=FakeExecutor(text="Цены выросли на 3%."),
                  receivers=receivers, sleeper=lambda s: None)


def test_the_report_of_a_scheduled_run_goes_to_both_messengers(config, store):
    tg_in = FakeReceiver("telegram")
    mx_in = FakeReceiver("max", limit=4000)
    bridge = bridge_with(config, store, {"telegram": tg_in, "max": mx_in})
    # В Max человек тоже здоровался — мосту есть куда там говорить.
    store.upsert_link("max", 900, 0, project="analitika", session_id="s-max")
    bridge.router.handle(tg("каждое утро в 7:30 собирай сводку по моему делу"))
    bridge.alarm.launch(store.get_schedule(1))

    bridge.pool.wait_idle(timeout=5)
    bridge.deliver()
    assert any("Задача 1" in text for _chat, text in tg_in.sent)
    assert any("Задача 1" in text for _chat, text in mx_in.sent)


def test_a_scheduled_run_says_nothing_when_no_messenger_is_set_up(config, store):
    bridge = bridge_with(config, store, {})
    link = store.upsert_link("telegram", 500, 0, project="buhgalter", session_id="s")
    task = store.add_schedule(link["id"], "daily 07:30", "собирай сводку",
                              project="buhgalter")
    assert bridge.alarm.launch(store.get_schedule(task)) is not None

    bridge.pool.wait_idle(timeout=5)
    assert bridge.deliver() == 1              # работа разобрана, а сказать некому
    assert store.get_job(bridge.store.list_jobs()[0]["id"])["state"] == "done"


def test_an_ordinary_answer_still_goes_only_where_it_was_asked(config, store):
    tg_in = FakeReceiver("telegram")
    mx_in = FakeReceiver("max", limit=4000)
    bridge = bridge_with(config, store, {"telegram": tg_in, "max": mx_in})
    bridge.router.handle(tg("посчитай выручку"))

    bridge.pool.wait_idle(timeout=5)
    bridge.deliver()
    assert tg_in.sent and mx_in.sent == []


def test_the_bridge_looks_at_the_clock_on_every_loop(config, store):
    bridge = bridge_with(config, store, {"telegram": FakeReceiver("telegram")})
    bridge.tick()
    assert bridge.alarm.looks == 1


def test_the_help_text_tells_about_the_schedule(router):
    answer = said(router, "помощь")
    assert "каждое утро" in answer
    assert "расписание" in answer
