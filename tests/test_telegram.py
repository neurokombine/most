"""Приёмник Telegram: long polling, персистентный offset, три класса ошибок."""
import pytest

from bridge.receivers.base import (Attachment, BridgeConflict, FileTooBig,
                                   RateLimited, TokenRejected)
from bridge.receivers.telegram import TelegramReceiver
from tests.fakes import FakeResponse, FakeSession


def update(update_id, text="привет", user_id=111, chat_id=500):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id, "first_name": "Натэла"},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def make(store, responses):
    return TelegramReceiver(token="123:abc", store=store, session=FakeSession(responses))


def test_updates_become_incoming(store):
    r = make(store, [FakeResponse(200, {"ok": True, "result": [update(1, "посчитай")]})])
    incoming = r.poll_once()
    assert len(incoming) == 1
    msg = incoming[0]
    assert msg.channel == "telegram"
    assert msg.chat_id == 500
    assert msg.user_id == 111
    assert msg.text == "посчитай"
    assert msg.thread_id == 0


def test_offset_is_persisted_after_each_update(store):
    r = make(store, [FakeResponse(200, {"ok": True, "result": [update(7), update(8)]})])
    r.poll_once()
    assert store.get_setting("telegram_offset") == "9"


def test_offset_is_sent_on_the_next_poll(store):
    store.set_setting("telegram_offset", "42")
    session = FakeSession([FakeResponse(200, {"ok": True, "result": []})])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    r.poll_once()
    assert session.calls[0]["params"]["offset"] == 42


def test_conflict_409_stops_the_loop(store):
    r = make(store, [FakeResponse(409, {"ok": False, "description": "Conflict"})])
    with pytest.raises(BridgeConflict):
        r.poll_once()


def test_401_and_403_mean_token_rejected(store):
    for code in (401, 403):
        r = make(store, [FakeResponse(code, {"ok": False, "description": "Unauthorized"})])
        with pytest.raises(TokenRejected):
            r.poll_once()


def test_429_carries_retry_after(store):
    r = make(store, [FakeResponse(200, {"ok": False, "description": "Too Many Requests",
                                        "parameters": {"retry_after": 17}})])
    with pytest.raises(RateLimited) as exc:
        r.poll_once()
    assert exc.value.retry_after == 17


def test_non_json_answer_is_skipped_without_moving_offset(store):
    store.set_setting("telegram_offset", "5")
    r = make(store, [FakeResponse(502, payload=None, text="<html>bad gateway</html>")])
    assert r.poll_once() == []
    assert store.get_setting("telegram_offset") == "5"


def test_update_without_text_is_ignored_but_offset_moves(store):
    """Наклейка — не текст и не файл: сказать нечего, но смещение сдвигаем."""
    r = make(store, [FakeResponse(200, {"ok": True, "result": [
        {"update_id": 3, "message": {"from": {"id": 1}, "chat": {"id": 2},
                                     "sticker": {"file_id": "x"}}}]})])
    assert r.poll_once() == []
    assert store.get_setting("telegram_offset") == "4"


def test_thread_id_is_read_when_the_chat_has_topics(store):
    u = update(11)
    u["message"]["message_thread_id"] = 77
    r = make(store, [FakeResponse(200, {"ok": True, "result": [u]})])
    assert r.poll_once()[0].thread_id == 77


def test_send_splits_long_text_into_chunks(store):
    session = FakeSession([FakeResponse(200, {"ok": True}), FakeResponse(200, {"ok": True})])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    r.send(500, "я" * 5000)
    assert len(session.calls) == 2
    assert all(len(c["data"]["text"]) <= 4096 for c in session.calls)


def test_token_is_never_printed_in_an_error(store):
    r = make(store, [FakeResponse(401, {"ok": False, "description": "bot 123:abc revoked"})])
    with pytest.raises(TokenRejected) as exc:
        r.poll_once()
    assert "123:abc" not in str(exc.value)


def test_404_means_token_rejected_not_a_pause(store):
    """Telegram отвечает 404 на несуществующий или пустой токен: адрес бота
    просто не существует. Ждать тут нечего — это чинится руками."""
    r = make(store, [FakeResponse(404, {"ok": False, "description": "Not Found"})])
    with pytest.raises(TokenRejected):
        r.poll_once()


# --- этап 3: файлы туда и обратно -------------------------------------------

def with_document(update_id=20, name="отчёт за сентябрь.xlsx", size=1024, caption=None):
    message = {
        "message_id": update_id,
        "from": {"id": 111},
        "chat": {"id": 500, "type": "private"},
        "document": {"file_id": "BQACAgIA-file", "file_unique_id": "u1",
                     "file_name": name, "mime_type": "application/vnd.ms-excel",
                     "file_size": size},
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def with_photo(update_id=21, caption=None):
    message = {
        "message_id": update_id,
        "from": {"id": 111},
        "chat": {"id": 500, "type": "private"},
        "photo": [{"file_id": "small", "file_unique_id": "s", "width": 90,
                   "height": 60, "file_size": 900},
                  {"file_id": "big", "file_unique_id": "b", "width": 1280,
                   "height": 720, "file_size": 90000}],
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def test_document_becomes_an_attachment(store):
    r = make(store, [FakeResponse(200, {"ok": True, "result": [with_document()]})])
    msg = r.poll_once()[0]
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.kind == "file"
    assert att.file_name == "отчёт за сентябрь.xlsx"
    assert att.size == 1024
    assert att.file_id == "BQACAgIA-file"
    assert msg.text == ""


def test_caption_becomes_the_task(store):
    r = make(store, [FakeResponse(200, {"ok": True, "result":
                                        [with_document(caption="посчитай итог по этой таблице")]})])
    msg = r.poll_once()[0]
    assert msg.text == "посчитай итог по этой таблице"
    assert msg.attachments


def test_photo_takes_the_biggest_size(store):
    r = make(store, [FakeResponse(200, {"ok": True, "result": [with_photo()]})])
    att = r.poll_once()[0].attachments[0]
    assert att.file_id == "big"
    assert att.kind == "photo"
    assert att.file_name == ""


def test_video_and_audio_are_attachments_too(store):
    updates = []
    for number, (kind, body) in enumerate((
            ("video", {"file_id": "v", "file_unique_id": "v", "file_size": 10,
                       "file_name": "ролик.mp4"}),
            ("audio", {"file_id": "a", "file_unique_id": "a", "file_size": 10,
                       "file_name": "звук.mp3"}))):
        updates.append({"update_id": 30 + number, "message": {
            "message_id": 30 + number, "from": {"id": 111},
            "chat": {"id": 500, "type": "private"}, kind: body}})
    r = make(store, [FakeResponse(200, {"ok": True, "result": updates})])
    kinds = [m.attachments[0].kind for m in r.poll_once()]
    assert kinds == ["video", "audio"]


def test_file_is_downloaded_through_get_file(store):
    session = FakeSession([
        FakeResponse(200, {"ok": True, "result": {"file_id": "BQACAgIA-file",
                                                  "file_path": "documents/file_7.xlsx",
                                                  "file_size": 1024}}),
        FakeResponse(200, content=b"body-of-the-file"),
    ])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    data = r.fetch(Attachment(kind="file", file_id="BQACAgIA-file", size=1024))
    assert data == b"body-of-the-file"
    assert "getFile" in session.calls[0]["url"]
    assert session.calls[1]["url"].endswith("/file/bot123:abc/documents/file_7.xlsx")


def test_file_over_twenty_megabytes_is_refused_before_the_request(store):
    session = FakeSession([])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    with pytest.raises(FileTooBig):
        r.fetch(Attachment(kind="file", file_id="x", size=25 * 1024 * 1024))
    assert session.calls == []          # в телеграм даже не ходили


def test_telegram_saying_file_is_too_big_is_the_same_trouble(store):
    session = FakeSession([FakeResponse(400, {"ok": False, "description": "file is too big"})])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    with pytest.raises(FileTooBig):
        r.fetch(Attachment(kind="file", file_id="x", size=0))


def test_download_trouble_never_shows_the_token(store):
    session = FakeSession([FakeResponse(400, {"ok": False,
                                              "description": "Bad Request: 123:abc is wrong"})])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    with pytest.raises(RuntimeError) as exc:
        r.fetch(Attachment(kind="file", file_id="x", size=0))
    assert "123:abc" not in str(exc.value)


def test_file_goes_out_as_a_document(store, tmp_path):
    path = tmp_path / "отчёт за сентябрь.xlsx"
    path.write_bytes("таблица".encode("utf-8"))
    session = FakeSession([FakeResponse(200, {"ok": True, "result": {"message_id": 5}})])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    r.send_file(500, path, caption="вот он")

    call = session.calls[0]
    assert "sendDocument" in call["url"]
    assert call["data"]["chat_id"] == 500
    assert call["data"]["caption"] == "вот он"
    assert "document" in call["files"]
    assert call["files"]["document"][0] == "отчёт за сентябрь.xlsx"


def test_photo_is_sent_as_a_document_not_as_a_photo(store, tmp_path):
    path = tmp_path / "кадр.jpg"
    path.write_bytes(b"jpeg")
    session = FakeSession([FakeResponse(200, {"ok": True, "result": {"message_id": 5}})])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    r.send_file(500, path)
    assert "sendDocument" in session.calls[0]["url"]     # без пережатия


def test_sending_a_file_over_fifty_megabytes_is_refused(store, tmp_path):
    path = tmp_path / "большой.bin"
    path.write_bytes(b"0")
    session = FakeSession([])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    r.upload_limit = 0
    with pytest.raises(FileTooBig):
        r.send_file(500, path)
    assert session.calls == []


# --- этап 4: голос ----------------------------------------------------------

def with_voice(update_id=40, duration=17, caption=None):
    message = {"message_id": update_id, "from": {"id": 111},
               "chat": {"id": 500, "type": "private"},
               "voice": {"file_id": "AwACAgIA-voice", "file_unique_id": "v",
                         "duration": duration, "mime_type": "audio/ogg",
                         "file_size": 30000}}
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def test_voice_message_becomes_an_attachment(store):
    r = make(store, [FakeResponse(200, {"ok": True, "result": [with_voice()]})])
    msg = r.poll_once()[0]
    att = msg.attachments[0]
    assert att.kind == "voice"
    assert att.file_id == "AwACAgIA-voice"
    assert att.duration == 17
    assert att.file_name == ""              # у голосового имени нет, его придумает почтальон


def test_voice_message_without_text_is_not_dropped_anymore(store):
    r = make(store, [FakeResponse(200, {"ok": True, "result": [with_voice()]})])
    assert len(r.poll_once()) == 1


def test_audio_file_carries_its_length_too(store):
    body = {"file_id": "a", "file_unique_id": "a", "file_size": 10,
            "file_name": "запись.mp3", "duration": 42}
    upd = {"update_id": 41, "message": {"message_id": 41, "from": {"id": 111},
                                        "chat": {"id": 500, "type": "private"},
                                        "audio": body}}
    r = make(store, [FakeResponse(200, {"ok": True, "result": [upd]})])
    att = r.poll_once()[0].attachments[0]
    assert att.kind == "audio"
    assert att.duration == 42


def test_ogg_answer_goes_out_as_a_voice_message(store, tmp_path):
    path = tmp_path / "otvet.ogg"
    path.write_bytes(b"OggS")
    session = FakeSession([FakeResponse(200, {"ok": True, "result": {}})])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    r.send_voice(500, path, caption="Готово")

    call = session.calls[0]
    assert call["url"].endswith("/sendVoice")
    assert "voice" in call["files"]
    assert call["data"]["chat_id"] == 500
    assert call["data"]["caption"] == "Готово"


def test_wav_answer_goes_out_as_audio_when_there_is_no_ogg(store, tmp_path):
    """Без ffmpeg запись остаётся wav — тогда это обычное аудио, а не кружок."""
    path = tmp_path / "otvet.wav"
    path.write_bytes(b"RIFF")
    session = FakeSession([FakeResponse(200, {"ok": True, "result": {}})])
    r = TelegramReceiver(token="123:abc", store=store, session=session)
    r.send_voice(500, path)

    call = session.calls[0]
    assert call["url"].endswith("/sendAudio")
    assert "audio" in call["files"]


def test_too_big_voice_answer_is_refused(store, tmp_path):
    path = tmp_path / "otvet.ogg"
    path.write_bytes(b"x")
    r = TelegramReceiver(token="123:abc", store=store, session=FakeSession([]))
    r.upload_limit = 0
    with pytest.raises(FileTooBig):
        r.send_voice(500, path)


def test_video_note_is_heard_as_a_voice_message(store):
    """Кружок — тоже речь, но файл у него mp4, а не ogg."""
    upd = {"update_id": 42, "message": {"message_id": 42, "from": {"id": 111},
                                        "chat": {"id": 500, "type": "private"},
                                        "video_note": {"file_id": "vn", "duration": 9,
                                                       "file_size": 500}}}
    r = make(store, [FakeResponse(200, {"ok": True, "result": [upd]})])
    att = r.poll_once()[0].attachments[0]
    assert att.kind == "video_note"
    assert att.duration == 9
