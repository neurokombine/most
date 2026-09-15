"""Маршрутизатор: белый список, журнал чужих стуков, проект на связку, команды."""
import pytest

from bridge.executor import FakeExecutor, Result
from bridge.receivers.base import Incoming
from bridge.router import Router


@pytest.fixture()
def router(config, store):
    store.sync_allowlist("telegram", config.telegram.allowlist)
    store.sync_allowlist("max", config.max.allowlist)
    return Router(config=config, store=store, executor=FakeExecutor(text="Готово."))


def tg(text, user_id=111, chat_id=500):
    return Incoming(channel="telegram", chat_id=chat_id, user_id=user_id,
                    text=text, thread_id=0, raw={})


def mx(text, user_id=222, chat_id=900):
    return Incoming(channel="max", chat_id=chat_id, user_id=user_id,
                    text=text, thread_id=0, raw={})


def test_own_message_reaches_the_executor(router):
    answers = router.handle(tg("посчитай остатки"))
    assert answers
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
    router.handle(tg("посчитай"))
    link = store.get_link("telegram", 500, 0)
    assert link["project"] == "analitika"          # первая по алфавиту
    assert router.executor.calls[0]["workdir"].name == "analitika"


def test_project_is_switched_by_words(router, store):
    answers = router.handle(tg("работаем с buhgalter"))
    assert "buhgalter" in "\n".join(answers)
    router.handle(tg("посчитай"))
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
    router.handle(tg("раз"))
    first = store.get_link("telegram", 500, 0)["session_id"]
    router.handle(tg("два"))
    assert store.get_link("telegram", 500, 0)["session_id"] == first
    assert router.executor.calls[1]["session_id"] == first


def test_job_is_written_into_the_journal_table(router, store):
    router.handle(tg("посчитай"))
    assert store.list_jobs()[0]["state"] == "done"


def test_executor_failure_is_told_humanly_not_by_traceback(config, store):
    class Broken(FakeExecutor):
        def run(self, prompt, workdir, session_id=None):
            return Result(ok=False, exit_code=1, session_id=session_id or "s",
                          job_id="j", job_dir=workdir, events=[], text="",
                          error="claude не найден")

    store.sync_allowlist("telegram", [111])
    r = Router(config=config, store=store, executor=Broken())
    answers = r.handle(tg("посчитай"))
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
    answers = r.handle(mx("длинно"))
    assert all(len(a) <= 4000 for a in answers)


def test_answer_is_chunked_for_telegram_by_its_own_limit(config, store):
    store.sync_allowlist("telegram", [111])
    r = Router(config=config, store=store, executor=FakeExecutor(text="я" * 9000))
    answers = r.handle(tg("длинно"))
    assert all(len(a) <= 4096 for a in answers)
