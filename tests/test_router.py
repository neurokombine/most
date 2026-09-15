"""Маршрутизатор: белый список, журнал чужих стуков, проект на связку, команды."""
import pytest

from pathlib import Path

from bridge.executor import FakeExecutor, Result
from bridge.receivers.base import Attachment, FileTooBig, Incoming
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


def test_short_budget_is_told_in_seconds_not_in_one_minute(config, store):
    store.sync_allowlist("telegram", [111])
    executor = FakeExecutor(delay=60, timeout=10, partial="Начала.")
    r = Router(config=config, store=store, executor=executor)
    text = "\n".join(done(r, tg("посчитай")))
    assert "за 10 с" in text
    assert "1 мин" not in text


# --- этап 3: файлы туда и обратно -------------------------------------------

class FakeReceiver:
    """Приёмник для тестов: файлы «скачивает» из памяти и складывает отправленное."""

    channel = "telegram"
    limit = 4096
    download_limit = 20 * 1024 * 1024
    upload_limit = 50 * 1024 * 1024

    def __init__(self, body=b"body", trouble=None):
        self.body = body
        self.trouble = trouble
        self.sent_files = []
        self.sent_texts = []
        self.sent_voices = []

    def fetch(self, attachment):
        if self.trouble is not None:
            raise self.trouble
        return self.body

    def send(self, chat_id, text):
        self.sent_texts.append((chat_id, text))

    def send_file(self, chat_id, path, caption=""):
        self.sent_files.append((chat_id, Path(path), caption))

    def send_voice(self, chat_id, path, caption=""):
        self.sent_voices.append((chat_id, Path(path), caption))


class FakePostbox:
    """Мост, умеющий говорить во все настроенные каналы разом."""

    def __init__(self, delivered=2):
        self.delivered = delivered
        self.calls = []

    def broadcast(self, text, file=None):
        self.calls.append((text, Path(file) if file else None))
        return self.delivered


def with_file(name="отчёт.xlsx", size=1024, caption="", channel="telegram"):
    att = Attachment(kind="file", file_id="f1", file_name=name, size=size)
    return Incoming(channel=channel, chat_id=500, user_id=111, text=caption,
                    thread_id=0, raw={}, attachments=[att])


def project_dir(router):
    return Path(router.config.projects_dir) / router.default_project()


def test_sent_file_lands_in_the_russian_folder(router):
    receiver = FakeReceiver(body=b"12345")
    answers = router.handle(with_file(), receiver=receiver)
    saved = project_dir(router) / "входящие" / "отчёт.xlsx"
    assert saved.exists()
    assert saved.read_bytes() == b"12345"
    said = "\n".join(answers)
    assert "входящие" in said and "отчёт.xlsx" in said


def test_the_answer_names_the_size(router):
    answers = router.handle(with_file(size=1024), receiver=FakeReceiver(body=b"x" * 2048))
    assert "2 КБ" in "\n".join(answers)


def test_cyrillic_project_folder_works(config, store, tmp_path):
    projects = tmp_path / "проекты мои"
    (projects / "бухгалтерия за год").mkdir(parents=True)
    config.projects_dir = projects
    store.sync_allowlist("telegram", config.telegram.allowlist)
    r = Router(config=config, store=store, executor=FakeExecutor())
    r.handle(with_file(name="акт сверки.pdf"), receiver=FakeReceiver())
    assert (projects / "бухгалтерия за год" / "входящие" / "акт сверки.pdf").exists()
    r.pool.stop_all()


def test_caption_becomes_a_task_with_the_path_inside(router):
    answers = router.handle(with_file(caption="посчитай итог по этой таблице"),
                            receiver=FakeReceiver())
    router.pool.wait_idle()
    prompt = router.executor.calls[0]["prompt"]
    assert "посчитай итог по этой таблице" in prompt
    assert "отчёт.xlsx" in prompt
    assert "входящие" in prompt
    assert "работу" in "\n".join(answers)          # и сказали, что взяли в работу


def test_file_without_a_caption_starts_no_work(router):
    router.handle(with_file(), receiver=FakeReceiver())
    router.pool.wait_idle()
    assert router.executor.calls == []


def test_file_too_big_is_told_honestly_and_nothing_is_saved(router):
    receiver = FakeReceiver(trouble=FileTooBig("не пущу", size=30 * 1024 * 1024,
                                               limit=20 * 1024 * 1024))
    answers = router.handle(with_file(size=30 * 1024 * 1024), receiver=receiver)
    said = "\n".join(answers)
    assert "20" in said
    assert not (project_dir(router) / "входящие").exists()


def test_trouble_while_taking_the_file_is_said_without_the_token(router, store):
    receiver = FakeReceiver(trouble=RuntimeError("не смог забрать файл: код 500"))
    answers = router.handle(with_file(), receiver=receiver)
    assert answers
    assert "код 500" not in "\n".join(answers)      # человеку — по-человечески
    assert store.recent_journal()


def test_where_did_you_put_it_is_answered_by_the_bridge_itself(router):
    router.handle(with_file(name="смета.pdf"), receiver=FakeReceiver())
    answers = router.handle(tg("куда ты положила то, что я прислала"),
                            receiver=FakeReceiver())
    said = "\n".join(answers)
    assert "смета.pdf" in said
    assert "входящие" in said
    assert router.executor.calls == []              # нейросеть для этого не нужна


# --- «пришли мне <файл>» ----------------------------------------------------

def make_file(router, name, body="x"):
    path = project_dir(router) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_send_me_the_file_finds_it_by_part_of_the_name(router):
    make_file(router, "Отчёт-Сентябрь.xlsx")
    receiver = FakeReceiver()
    router.handle(tg("пришли мне отчёт"), receiver=receiver)
    assert len(receiver.sent_files) == 1
    assert receiver.sent_files[0][1].name == "Отчёт-Сентябрь.xlsx"
    assert router.executor.calls == []              # это делает сам мост


def test_two_matches_ask_which_one(router):
    make_file(router, "отчёт-август.xlsx")
    make_file(router, "отчёт-сентябрь.xlsx")
    receiver = FakeReceiver()
    answers = router.handle(tg("пришли мне отчёт"), receiver=receiver)
    said = "\n".join(answers)
    assert receiver.sent_files == []
    assert "отчёт-август.xlsx" in said and "отчёт-сентябрь.xlsx" in said


def test_nothing_found_shows_what_there_is(router):
    make_file(router, "смета.pdf")
    answers = router.handle(tg("пришли мне накладную"), receiver=FakeReceiver())
    said = "\n".join(answers)
    assert "не нашла" in said.lower()
    assert "смета.pdf" in said


def test_file_without_a_name_asks_which_one(router):
    make_file(router, "смета.pdf")
    receiver = FakeReceiver()
    answers = router.handle(tg("пришли мне этот файл"), receiver=receiver)
    assert receiver.sent_files == []
    assert "смета.pdf" in "\n".join(answers)        # показали, из чего выбирать


def test_a_file_too_big_for_the_messenger_leaves_the_path(router):
    make_file(router, "большой.bin")
    receiver = FakeReceiver()
    receiver.send_file = _refuse(FileTooBig("не пущу", size=60 * 1024 * 1024,
                                            limit=50 * 1024 * 1024))
    answers = router.handle(tg("пришли мне большой"), receiver=receiver)
    said = "\n".join(answers)
    assert "большой.bin" in said                    # путь на сервере назвали
    assert "50" in said


def _refuse(trouble):
    def refuse(*args, **kwargs):
        raise trouble
    return refuse


def test_asking_for_a_file_in_an_empty_project_says_so(config, store, tmp_path):
    config.projects_dir = tmp_path / "пусто"
    store.sync_allowlist("telegram", config.telegram.allowlist)
    r = Router(config=config, store=store, executor=FakeExecutor())
    answers = r.handle(tg("пришли мне отчёт"), receiver=FakeReceiver())
    assert answers
    r.pool.stop_all()


def test_a_question_about_the_answer_is_not_a_file_request(router):
    router.handle(tg("пришли ответ целиком ещё раз"), receiver=FakeReceiver())
    router.pool.wait_idle()
    assert router.executor.calls          # ушло нейросети, а не в поиск файла


# --- «отдай» ----------------------------------------------------------------

def finish_work(router, text, creates=None):
    """Прогоняет одну работу с готовым ответом нейросети.

    Файл появляется ПОСЛЕ начала работы — как в жизни: мост верит папке,
    а не словам, и файл «старее» работы результатом не считается.
    """
    router.executor.text = text
    router.handle(tg("сделай отчёт"))
    if creates:
        make_file(router, creates)
    router.pool.wait_idle()
    out = []
    for work in router.pool.collect():
        out += router.finished_messages(work)
    return out


def test_the_bridge_remembers_the_file_the_neural_net_named(router):
    answers = finish_work(router, "Готово, сложила всё в итог-сентября.xlsx",
                          creates="итог-сентября.xlsx")
    assert "итог-сентября.xlsx" in "\n".join(answers)
    assert "отдай" in "\n".join(answers).lower()


def test_give_it_to_me_sends_the_last_result_into_every_messenger(router):
    finish_work(router, "Готово, сложила всё в итог-сентября.xlsx",
                creates="итог-сентября.xlsx")
    postbox = FakePostbox()
    router.postbox = postbox
    router.handle(tg("отдай"), receiver=FakeReceiver())
    assert len(postbox.calls) == 1
    assert postbox.calls[0][1].name == "итог-сентября.xlsx"


def test_give_it_to_me_without_a_result_asks_for_a_name(router):
    receiver = FakeReceiver()
    answers = router.handle(tg("отдай"), receiver=receiver)
    assert receiver.sent_files == []
    assert "пришли" in "\n".join(answers).lower()


def test_give_me_the_result_is_the_same_command(router):
    finish_work(router, "Готово, файл итог.xlsx лежит в папке", creates="итог.xlsx")
    postbox = FakePostbox()
    router.postbox = postbox
    router.handle(tg("пришли результат"), receiver=FakeReceiver())
    assert postbox.calls


def test_a_named_file_goes_only_where_it_was_asked(router):
    make_file(router, "смета.pdf")
    postbox = FakePostbox()
    router.postbox = postbox
    receiver = FakeReceiver()
    router.handle(tg("пришли мне смету"), receiver=receiver)
    assert postbox.calls == []              # спросили в одном — ответ там же
    assert len(receiver.sent_files) == 1


def test_a_file_only_promised_is_not_remembered(router):
    """Сказала «сохранила в отчёт.xlsx», а файла нет — отдавать нечего."""
    answers = finish_work(router, "Готово, сохранила всё в отчёт.xlsx")
    assert "отдай" not in "\n".join(answers).lower()
    receiver = FakeReceiver()
    later = router.handle(tg("отдай"), receiver=receiver)
    assert receiver.sent_files == []
    assert "пришли" in "\n".join(later).lower()


def test_the_failure_names_the_step_it_stumbled_on(router, config):
    """«Не вышло» без причины нечего показать нейросети.

    Живая приёмка 15.09: файл дважды не ушёл в Max, и в чате была только
    строчка «отправить не вышло». Причина (`upload.error`) лежала в журнале,
    а человек её не видел и позвать на помощь не мог.
    """
    make_file(router, "смета.pdf")
    receiver = FakeReceiver()
    receiver.send_file = _refuse(RuntimeError("не смог залить файл в Max: upload.error"))
    answers = router.handle(tg("пришли мне смету"), receiver=receiver)

    text = "\n".join(answers)
    assert "upload.error" in text
    assert "смета.pdf" in text


def test_the_failure_reason_is_cleaned_of_the_token(router, store, config):
    """Причину показываем человеку — значит, чистим её так же, как журнал."""
    make_file(router, "смета.pdf")
    receiver = FakeReceiver()
    receiver.send_file = _refuse(RuntimeError(f"/bot{config.telegram.token}/sendDocument упал"))
    answers = router.handle(tg("пришли мне смету"), receiver=receiver)
    assert config.telegram.token not in "\n".join(answers)


def test_the_token_never_reaches_the_journal(router, store, config):
    """Сеть любит вписать в ошибку полный адрес запроса, а в нём — токен."""
    make_file(router, "смета.pdf")
    receiver = FakeReceiver()
    receiver.send_file = _refuse(RuntimeError(
        f"HTTPSConnectionPool: /bot{config.telegram.token}/sendDocument"))
    answers = router.handle(tg("пришли мне смету"), receiver=receiver)

    written = "\n".join(str(row["text"]) for row in store.recent_journal())
    assert config.telegram.token not in written
    assert "***" in written
    assert config.telegram.token not in "\n".join(answers)


def test_the_token_never_reaches_the_journal_when_taking_a_file(router, store, config):
    receiver = FakeReceiver(trouble=RuntimeError(f"bot{config.telegram.token} упал"))
    router.handle(with_file(), receiver=receiver)
    written = "\n".join(str(row["text"]) for row in store.recent_journal())
    assert config.telegram.token not in written


# --- фразы урока Б.6: как их произносят на камеру ---------------------------
# Эти три строки взяты из съёмочного листа и из промта ученика. Пока их не было
# в разборе словами, мост отправлял их нейросети как задачу — то есть жёг
# подписку на команду, которую умеет сам.

def test_a_folder_is_switched_even_with_a_lead_in_word_and_a_full_stop(router, store):
    answers = router.handle(tg("Дальше работаем с папкой buhgalter."))
    assert "buhgalter" in "\n".join(answers)
    assert store.get_link("telegram", 500, 0)["project"] == "buhgalter"


def test_projects_are_listed_when_asked_in_plain_words(router):
    for said in ("Покажи, какие у меня есть папки.",
                 "какие у меня папки",
                 "какие есть проекты"):
        text = "\n".join(router.handle(tg(said)))
        assert "buhgalter" in text and "analitika" in text, said


def test_asking_for_the_summary_is_not_a_request_for_a_file(router):
    text = "\n".join(router.handle(tg("пришли сводку")))
    assert "нет ни одного файла" not in text


# --- группы (живая приёмка 15.09) -------------------------------------------

def group(text, user_id=999, chat_id=-1002186710990, channel="telegram"):
    return Incoming(channel=channel, chat_id=chat_id, user_id=user_id, text=text,
                    thread_id=0, name="Евгения Косых", group=True, raw={})


def test_a_group_gets_silence_and_stays_out_of_the_knocks(router, store):
    """Бот живёт в чатах учеников; «кто стучался» — не список этих чатов."""
    assert router.handle(group("Сколько желающих")) == []
    assert store.recent_strangers() == []


def test_a_group_is_noted_apart_from_the_strangers(router, store):
    router.handle(group("Сколько желающих"))
    noted = [row for row in store.recent_journal() if row["kind"] == "group"]
    assert len(noted) == 1
    assert noted[0]["chat_id"] == -1002186710990


def test_the_same_group_is_noted_only_once(router, store):
    for word in ("раз", "два", "три"):
        router.handle(group(word))
    noted = [row for row in store.recent_journal() if row["kind"] == "group"]
    assert len(noted) == 1


def test_even_an_own_person_is_not_answered_in_a_group(router):
    """Мост в уроке — личный разговор, а не общий чат."""
    assert router.handle(group("посчитай остатки", user_id=111)) == []
    assert router.executor.calls == []


def test_the_summary_does_not_count_a_group(router, store):
    router.handle(group("привет всем"))
    assert store.strangers_since("2000-01-01T00:00:00+00:00") == []


# --- вежливость (живая приёмка 15.09) ---------------------------------------

def test_hello_is_answered_by_the_bridge_itself(router):
    """Пять секунд ожидания и деньги подписки за «Чем могу помочь» — это перебор."""
    answers = router.handle(tg("привет"))
    assert answers and answers[0].strip()
    assert router.executor.calls == []


def test_the_first_hello_tells_what_the_bridge_can_do(router):
    first = "\n".join(router.handle(tg("привет")))
    assert len(first.splitlines()) >= 3
    assert "помощь" in first.lower()


def test_the_second_hello_is_one_line(router):
    router.handle(tg("привет"))
    second = "\n".join(router.handle(tg("здравствуйте")))
    assert len(second.splitlines()) == 1
    assert router.executor.calls == []


def test_each_person_hears_the_long_hello_once(router):
    router.handle(tg("привет"))
    other = "\n".join(router.handle(mx("привет")))
    assert len(other.splitlines()) >= 3


@pytest.mark.parametrize("word", ["спасибо", "Спасибо большое", "спс", "благодарю",
                                  "ок", "окей", "поняла", "ясно", "принято"])
def test_short_polite_words_are_answered_without_the_executor(router, word):
    answers = router.handle(tg(word))
    assert answers and answers[0].strip()
    assert router.executor.calls == []


@pytest.mark.parametrize("word", ["ты тут?", "ты здесь", "ты на месте?", "ты живая?"])
def test_are_you_there_is_answered_at_once(router, word):
    answers = router.handle(tg(word))
    assert answers and answers[0].strip()
    assert router.executor.calls == []


@pytest.mark.parametrize("text", ["привет, посчитай остатки",
                                  "спасибо, а теперь посчитай остатки",
                                  "ок, запусти проверку"])
def test_a_greeting_with_a_task_still_goes_to_work(router, text):
    router.handle(tg(text))
    router.pool.wait_idle()
    assert router.executor.calls, text


def test_a_plain_yes_is_not_eaten_by_the_bridge(router):
    """«Да» — это чаще всего ответ нейросети на её же вопрос, а не вежливость."""
    router.handle(tg("да"))
    router.pool.wait_idle()
    assert router.executor.calls
