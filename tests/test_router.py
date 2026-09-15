"""Маршрутизатор: белый список, журнал чужих стуков, проект на связку, команды."""
import pytest

from bridge.executor import FakeExecutor, Result
from bridge.receivers.base import Incoming
from bridge.router import Router


@pytest.fixture()
def router(config, store):
    store.sync_allowlist("telegram", config.telegram.allowlist)
    store.sync_allowlist("max", config.max.allowlist)
    r = Router(config=config, store=store, executor=FakeExecutor(text="Готово."))
    yield r
    r.pool.stop_all()
    r.pool.wait_idle()


def done(router, message):
    """Задача уходит в очередь; ждём её и берём то, что мост скажет в чат."""
    router.handle(message)
    router.pool.wait_idle()
    out = []
    for work in router.pool.collect():
        out += router.finished_messages(work)
    return out


def tg(text, user_id=111, chat_id=500):
    return Incoming(channel="telegram", chat_id=chat_id, user_id=user_id,
                    text=text, thread_id=0, raw={})


def mx(text, user_id=222, chat_id=900):
    return Incoming(channel="max", chat_id=chat_id, user_id=user_id,
                    text=text, thread_id=0, raw={})


def test_own_message_reaches_the_executor(router):
    accepted = router.handle(tg("посчитай остатки"))
    assert "работу" in "\n".join(accepted)          # сразу говорим, что взяли
    router.pool.wait_idle()
    answers = [t for w in router.pool.collect() for t in router.finished_messages(w)]
    assert "Готово." in "\n".join(answers)
    assert router.executor.calls[0]["prompt"] == "посчитай остатки"


def test_stranger_gets_silence_and_a_journal_line(router, store):
    assert router.handle(tg("пусти меня", user_id=999)) == []
    knocks = store.recent_strangers()
    assert len(knocks) == 1
    assert knocks[0]["user_id"] == 999
    assert knocks[0]["channel"] == "telegram"
    assert knocks[0]["text"].startswith("пусти меня")


def test_empty_allowlist_answers_nobody(config, store):
    store.sync_allowlist("telegram", [])
    r = Router(config=config, store=store, executor=FakeExecutor())
    assert r.handle(tg("эй")) == []
    assert store.recent_strangers()[0]["user_id"] == 111


def test_allowlist_of_one_channel_does_not_open_the_other(router):
    assert router.handle(mx("привет", user_id=111)) == []
    assert router.handle(mx("привет", user_id=222)) != []


def test_who_knocked_shows_the_last_strangers(router):
    router.handle(tg("я чужой", user_id=999))
    answers = router.handle(tg("покажи, кто стучался"))
    text = "\n".join(answers)
    assert "999" in text
    assert "я чужой" in text


def test_who_knocked_on_an_empty_journal_is_honest(router):
    answers = router.handle(tg("кто стучался"))
    assert answers
    assert "никто" in "\n".join(answers).lower()


def test_default_project_is_the_first_folder(router, store):
    done(router, tg("посчитай"))
    link = store.get_link("telegram", 500, 0)
    assert link["project"] == "analitika"          # первая по алфавиту
    assert router.executor.calls[0]["workdir"].name == "analitika"


def test_project_is_switched_by_words(router, store):
    answers = router.handle(tg("работаем с buhgalter"))
    assert "buhgalter" in "\n".join(answers)
    done(router, tg("посчитай"))
    assert store.get_link("telegram", 500, 0)["project"] == "buhgalter"
    assert router.executor.calls[-1]["workdir"].name == "buhgalter"


def test_switching_to_a_missing_folder_is_refused_humanly(router, store):
    answers = router.handle(tg("работаем с nesushchestvuyushchaya"))
    text = "\n".join(answers)
    assert "nesushchestvuyushchaya" in text
    assert "buhgalter" in text          # подсказали, что есть
    assert store.get_link("telegram", 500, 0) is None or \
        store.get_link("telegram", 500, 0)["project"] != "nesushchestvuyushchaya"


def test_show_projects_lists_folders_and_marks_current(router):
    router.handle(tg("работаем с buhgalter"))
    text = "\n".join(router.handle(tg("покажи проекты")))
    assert "buhgalter" in text and "analitika" in text


def test_two_channels_keep_their_own_projects(router, store):
    router.handle(tg("работаем с buhgalter"))
    router.handle(mx("работаем с analitika"))
    assert store.get_link("telegram", 500, 0)["project"] == "buhgalter"
    assert store.get_link("max", 900, 0)["project"] == "analitika"


def test_session_id_is_born_once_and_stored(router, store):
    done(router, tg("раз"))
    first = store.get_link("telegram", 500, 0)["session_id"]
    done(router, tg("два"))
    assert store.get_link("telegram", 500, 0)["session_id"] == first
    assert router.executor.calls[1]["session_id"] == first


def test_job_is_written_into_the_journal_table(router, store):
    done(router, tg("посчитай"))
    assert store.list_jobs()[0]["state"] == "done"


def test_executor_failure_is_told_humanly_not_by_traceback(config, store):
    class Broken(FakeExecutor):
        def run(self, prompt, workdir, session_id=None, resume=False, handle=None):
            return Result(ok=False, exit_code=1, session_id=session_id or "s",
                          job_id="j", job_dir=workdir, events=[], text="",
                          error="claude не найден")

    store.sync_allowlist("telegram", [111])
    r = Router(config=config, store=store, executor=Broken())
    answers = done(r, tg("посчитай"))
    assert answers
    assert "claude" in "\n".join(answers)
    assert "Traceback" not in "\n".join(answers)
    assert store.list_jobs()[0]["state"] == "failed"


def test_empty_message_is_not_sent_to_the_executor(router):
    assert router.handle(tg("   ")) == []
    assert router.executor.calls == []


def test_answer_is_chunked_for_max_by_its_own_limit(config, store):
    store.sync_allowlist("max", [222])
    r = Router(config=config, store=store, executor=FakeExecutor(text="я" * 9000))
    answers = done(r, mx("длинно"))
    assert answers and all(len(a) <= 4000 for a in answers)


def test_answer_is_chunked_for_telegram_by_its_own_limit(config, store):
    store.sync_allowlist("telegram", [111])
    r = Router(config=config, store=store, executor=FakeExecutor(text="я" * 9000))
    answers = done(r, tg("длинно"))
    assert answers and all(len(a) <= 4096 for a in answers)


# --- этап 2: продолжение разговора, очередь, «стоп», «что получилось» ---------

def test_second_message_continues_the_same_conversation(router, store):
    done(router, tg("запомни: кодовое слово гранат"))
    done(router, tg("какое кодовое слово"))
    calls = router.executor.calls
    assert calls[0]["resume"] is False
    assert calls[1]["resume"] is True
    assert calls[1]["session_id"] == calls[0]["session_id"]


def test_lost_conversation_is_admitted_in_one_honest_phrase(config, store):
    store.sync_allowlist("telegram", [111])
    r = Router(config=config, store=store,
               executor=FakeExecutor(text="Готово.", lose_session=True))
    done(r, tg("раз"))                       # первая работа заводит сессию
    answers = done(r, tg("два"))             # вторая идёт с --resume и не находит её
    text = "\n".join(answers)
    assert "не нашёлся" in text
    assert "Готово." in text                 # но задачу всё равно сделали
    r.pool.stop_all()


def test_new_conversation_by_words_forgets_the_old_session(router, store):
    done(router, tg("раз"))
    before = store.get_link("telegram", 500, 0)
    answers = router.handle(tg("новый разговор"))
    after = store.get_link("telegram", 500, 0)
    assert "новый разговор" in "\n".join(answers).lower()
    assert after["session_id"] != before["session_id"]
    assert after["session_started"] == 0
    assert after["project"] == before["project"]      # папка та же

    done(router, tg("два"))
    assert router.executor.calls[-1]["resume"] is False


def test_second_message_while_working_is_answered_wait_not_silence(config, store):
    store.sync_allowlist("telegram", [111])
    r = Router(config=config, store=store, executor=FakeExecutor(delay=5))
    r.handle(tg("долгая задача"))
    answers = r.handle(tg("а ещё вот это"))
    text = "\n".join(answers)
    assert "работаю" in text and "стоп" in text
    assert len(r.executor.calls) == 1                 # вторую в работу не взяли
    r.pool.stop_all()
    r.pool.wait_idle()


def test_two_chats_work_in_parallel_when_allowed(config, store):
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    config.parallel = 2
    r = Router(config=config, store=store, executor=FakeExecutor(delay=5))
    r.handle(tg("долгая задача"))
    r.handle(mx("и моя тоже"))
    assert r.pool.running() == 2
    r.pool.stop_all()
    r.pool.wait_idle()


def test_over_the_parallel_limit_the_answer_is_honest(config, store):
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    config.parallel = 1
    r = Router(config=config, store=store, executor=FakeExecutor(delay=5))
    r.handle(tg("долгая задача"))
    text = "\n".join(r.handle(mx("и моя тоже")))
    assert "занята" in text
    r.pool.stop_all()
    r.pool.wait_idle()


def test_stop_ends_the_work_and_tells_what_it_managed(config, store):
    store.sync_allowlist("telegram", [111])
    r = Router(config=config, store=store,
               executor=FakeExecutor(delay=5, partial="Успела посчитать август."))
    r.handle(tg("долгая задача"))
    assert r.handle(tg("стоп")) == []                 # молчим: сейчас скажет сама работа
    r.pool.wait_idle()
    text = "\n".join(t for w in r.pool.collect() for t in r.finished_messages(w))
    assert "Остановила по вашей просьбе" in text
    assert "август" in text
    assert store.list_jobs()[0]["state"] == "stopped"


def test_stop_with_nothing_running_is_answered_not_ignored(router):
    assert "нечего" in "\n".join(router.handle(tg("стоп")))


def test_time_budget_answer_says_how_long_and_what_it_managed(config, store):
    store.sync_allowlist("telegram", [111])
    executor = FakeExecutor(delay=5, timeout=0.3, partial="Начала считать август.")
    executor.timeout = 0.3
    r = Router(config=config, store=store, executor=executor)
    answers = done(r, tg("посчитай всё"))
    text = "\n".join(answers)
    assert "уложилась" in text
    assert "август" in text
    assert store.list_jobs()[0]["state"] == "timeout"


def test_show_what_came_out_lists_files_newer_than_the_last_work(router, store, projects_dir):
    done(router, tg("посчитай"))
    project = projects_dir / store.get_link("telegram", 500, 0)["project"]
    (project / "otchet.md").write_text("готово", encoding="utf-8")

    text = "\n".join(router.handle(tg("покажи, что получилось")))
    assert "otchet.md" in text
    assert "МСК" in text


def test_show_what_came_out_is_honest_when_nothing_changed(router, store):
    done(router, tg("посчитай"))
    text = "\n".join(router.handle(tg("покажи, что получилось")))
    assert "ничего не изменилось" in text


def test_show_what_came_out_before_any_work_is_explained(router):
    assert "ещё ничего не делали" in "\n".join(router.handle(tg("что получилось")))


def test_what_did_you_do_shows_the_last_five_works(router, store):
    for n in range(7):
        done(router, tg(f"задача {n}"))
    text = "\n".join(router.handle(tg("что ты делала")))
    lines = [line for line in text.split("\n") if line.startswith("•")]
    assert len(lines) == 5
    assert "задача 6" in text
    assert "задача 1" not in text
    assert "готово" in text
    assert "МСК" in text


def test_what_did_you_do_on_an_empty_chat_is_honest(router):
    assert "ещё ничего не делала" in "\n".join(router.handle(tg("что ты делала")))
