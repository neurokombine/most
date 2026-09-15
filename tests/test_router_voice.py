"""Голос в маршрутизаторе: услышать записанное и, если просят, ответить вслух.

Ни модели, ни piper здесь нет: слух и голос подставные. Проверяем поведение
моста — что он говорит человеку, куда кладёт запись и что уходит нейросети.
"""
import pytest

from bridge import postman, texts, voice
from bridge.executor import FakeExecutor
from bridge.receivers.base import Attachment, Incoming
from bridge.router import Router
from tests.test_router import FakeReceiver, FakePostbox, tg


def make_router(config, store, ears=None, mouth=None, postbox=None):
    store.sync_allowlist("telegram", config.telegram.allowlist)
    store.sync_allowlist("max", config.max.allowlist)
    return Router(config=config, store=store, executor=FakeExecutor(text="Готово."),
                  ears=ears or voice.FakeTranscriber(text="посчитай остатки"),
                  mouth=mouth or voice.FakeSpeaker(), postbox=postbox)


@pytest.fixture()
def router(config, store):
    r = make_router(config, store)
    yield r
    r.pool.stop_all()
    r.pool.wait_idle()


def with_voice(duration=17, caption="", channel="telegram", kind="voice"):
    return Incoming(channel=channel, chat_id=500 if channel == "telegram" else 900,
                    user_id=111 if channel == "telegram" else 222, text=caption,
                    attachments=[Attachment(kind=kind, file_id="voice-1",
                                            size=30000, duration=duration)])


def answers_of(router, message, receiver=None):
    return router.handle(message, receiver=receiver or FakeReceiver(body=b"OggS"))


# --- голос на вход ----------------------------------------------------------

def test_voice_lands_in_the_voice_folder(router, projects_dir):
    answers_of(router, with_voice())
    folder = projects_dir / "analitika" / postman.INBOX / postman.VOICE_DIR
    saved = list(folder.iterdir())
    assert len(saved) == 1
    assert saved[0].suffix == ".ogg"
    assert saved[0].read_bytes() == b"OggS"


def test_bridge_repeats_what_it_heard_before_working(router):
    answers = answers_of(router, with_voice())
    assert "слышала" in answers[0].lower()
    assert "посчитай остатки" in answers[0]


def test_transcription_becomes_the_task(router):
    answers_of(router, with_voice())
    router.pool.wait_idle()
    assert router.executor.calls[0]["prompt"] == "посчитай остатки"


def test_heard_line_is_cut_to_two_hundred_characters(config, store):
    long_speech = "сентябрь " * 100
    r = make_router(config, store, ears=voice.FakeTranscriber(text=long_speech))
    answers = answers_of(r, with_voice())
    r.pool.stop_all()
    r.pool.wait_idle()
    assert "…" in answers[0]
    assert len(answers[0]) < 300


def test_caption_next_to_the_voice_is_kept(router):
    answers_of(router, with_voice(caption="только по бухгалтерии"))
    router.pool.wait_idle()
    prompt = router.executor.calls[0]["prompt"]
    assert "только по бухгалтерии" in prompt
    assert "посчитай остатки" in prompt


def test_voice_in_max_works_the_same_way(config, store):
    r = make_router(config, store)
    answers = answers_of(r, with_voice(channel="max", kind="audio"))
    r.pool.wait_idle()
    assert "слышала" in answers[0].lower()
    assert r.executor.calls[0]["prompt"] == "посчитай остатки"
    r.pool.stop_all()


# --- предел длины -----------------------------------------------------------

def test_recording_longer_than_three_minutes_is_refused(router):
    ears = router.ears
    answers = answers_of(router, with_voice(duration=200))
    text = "\n".join(answers)
    assert "трёх минут" in text
    assert "текстом" in text
    assert ears.calls == []              # расшифровывать не начинали


def test_length_unknown_beforehand_is_caught_by_the_transcriber(config, store):
    """Max длину записи может и не назвать — тогда её ловит распознаватель."""
    r = make_router(config, store, ears=voice.FakeTranscriber(text="…", seconds=400))
    answers = answers_of(r, with_voice(duration=0, channel="max", kind="audio"))
    assert "трёх минут" in "\n".join(answers)


# --- деградация -------------------------------------------------------------

def test_without_the_library_bridge_asks_for_text(config, store, projects_dir):
    r = make_router(config, store, ears=voice.FakeTranscriber(available=False))
    answers = answers_of(r, with_voice())
    text = "\n".join(answers)
    assert "не настроен" in text
    assert "текстом" in text
    # запись всё равно сохранена: человек её уже наговорил, терять нельзя
    folder = projects_dir / "analitika" / postman.INBOX / postman.VOICE_DIR
    assert list(folder.iterdir())


def test_silence_is_not_a_task(config, store):
    r = make_router(config, store, ears=voice.FakeTranscriber(text="   "))
    answers = answers_of(r, with_voice())
    assert "не разобрала" in "\n".join(answers).lower() or \
           "не расслышала" in "\n".join(answers).lower()
    assert r.executor.calls == []


def test_voice_command_works_like_a_written_one(config, store):
    """«Стоп», сказанное голосом, останавливает работу, а не уходит заданием."""
    r = make_router(config, store, ears=voice.FakeTranscriber(text="стоп"))
    answers = answers_of(r, with_voice())
    text = "\n".join(answers)
    assert texts.STOP_NOTHING_TO_STOP in text
    assert r.executor.calls == []
    r.pool.stop_all()


# --- голос на выход ---------------------------------------------------------

def finish(router, receiver):
    router.pool.wait_idle()
    out = []
    for work in router.pool.collect():
        said = router.finished_messages(work)
        out += said
        out += router.voice_after_work(work, said, receiver)
    return out


def test_answer_is_read_aloud_when_asked(router):
    receiver = FakeReceiver()
    router.handle(tg("ответь голосом: сколько у нас осталось"), receiver=receiver)
    finish(router, receiver)
    assert router.mouth.said == ["Готово."]
    assert len(receiver.sent_voices) == 1
    assert receiver.sent_voices[0][0] == 500


def test_ordinary_answer_is_not_read_aloud(router):
    receiver = FakeReceiver()
    router.handle(tg("посчитай остатки"), receiver=receiver)
    finish(router, receiver)
    assert router.mouth.said == []
    assert receiver.sent_voices == []


def test_the_word_read_aloud_also_turns_the_voice_on(router):
    receiver = FakeReceiver()
    router.handle(tg("прочитай вслух, сколько у нас осталось"), receiver=receiver)
    finish(router, receiver)
    assert receiver.sent_voices


def test_voice_request_does_not_stay_for_the_next_task(router):
    receiver = FakeReceiver()
    router.handle(tg("ответь голосом: сколько осталось"), receiver=receiver)
    finish(router, receiver)
    router.handle(tg("а теперь по октябрю"), receiver=receiver)
    finish(router, receiver)
    assert len(receiver.sent_voices) == 1


def test_long_answer_is_read_only_to_the_limit(config, store):
    r = make_router(config, store)
    r.executor = FakeExecutor(text="о" * 4000)
    r.pool.executor = r.executor
    receiver = FakeReceiver()
    r.handle(tg("ответь голосом: расскажи всё"), receiver=receiver)
    said = finish(r, receiver)
    assert len(r.mouth.said[0]) <= voice.MAX_SPEAK_CHARS
    assert "о" * 3000 in "\n".join(said)          # текст ушёл целиком
    r.pool.stop_all()


def test_without_piper_bridge_says_it_once(config, store):
    r = make_router(config, store, mouth=voice.FakeSpeaker(available=False))
    receiver = FakeReceiver()
    r.handle(tg("ответь голосом: сколько осталось"), receiver=receiver)
    first = finish(r, receiver)
    assert "голосом" in "\n".join(first)
    assert receiver.sent_voices == []

    r.handle(tg("ответь голосом: а теперь по октябрю"), receiver=receiver)
    second = finish(r, receiver)
    # второй раз про ненастроенный голос не бубним
    assert "не настроен" not in "\n".join(second)
    r.pool.stop_all()


def test_voice_answer_trouble_does_not_eat_the_text(config, store):
    class Grumpy(FakeReceiver):
        def send_voice(self, chat_id, path, caption=""):
            raise RuntimeError("не ушло")

    r = make_router(config, store)
    receiver = Grumpy()
    r.handle(tg("ответь голосом: сколько осталось"), receiver=receiver)
    said = finish(r, receiver)
    assert "Готово." in "\n".join(said)
    r.pool.stop_all()


def test_video_note_goes_the_voice_way(router, projects_dir):
    """Кружок расшифровывается так же, как голосовое, и ложится mp4."""
    answers = answers_of(router, with_voice(kind="video_note"))
    assert "слышала" in answers[0].lower()
    saved = list((projects_dir / "analitika" / postman.INBOX / postman.VOICE_DIR).iterdir())
    assert saved[0].suffix == ".mp4"
