"""Приёмник Max: своя схема обновлений (marker, а не update_id), те же три класса ошибок."""
import pytest

from bridge.receivers.base import (Attachment, BridgeConflict, FileTooBig,
                                   RateLimited, TokenRejected)
from bridge.receivers.max import MaxReceiver, CA_BUNDLE
from tests.fakes import FakeResponse, FakeSession


def nested_update(text="привет", user_id=222, chat_id=900):
    """Схема message_created, как её отдаёт platform-api2.max.ru."""
    return {
        "update_type": "message_created",
        "timestamp": 1757900000000,
        "message": {
            "sender": {"user_id": user_id, "name": "Натэла"},
            "recipient": {"chat_id": chat_id, "chat_type": "dialog", "user_id": user_id},
            "body": {"mid": "mid-1", "seq": 1, "text": text},
        },
    }


def flat_update(text="привет", user_id=222, chat_id=900):
    """Плоская схема: update_type/chat_id/user на верхнем уровне."""
    return {"update_type": "message_created", "chat_id": chat_id,
            "user": {"user_id": user_id}, "text": text, "timestamp": 1757900000000}


def make(store, responses):
    return MaxReceiver(token="max-token", store=store, session=FakeSession(responses))


def test_nested_update_becomes_incoming(store):
    r = make(store, [FakeResponse(200, {"updates": [nested_update("посчитай")], "marker": 555})])
    incoming = r.poll_once()
    assert len(incoming) == 1
    assert incoming[0].channel == "max"
    assert incoming[0].chat_id == 900
    assert incoming[0].user_id == 222
    assert incoming[0].text == "посчитай"


def test_flat_update_becomes_incoming_too(store):
    r = make(store, [FakeResponse(200, {"updates": [flat_update("считай")], "marker": 1})])
    incoming = r.poll_once()
    assert len(incoming) == 1
    assert incoming[0].chat_id == 900
    assert incoming[0].text == "считай"


def test_marker_is_persisted_and_sent_back(store):
    r = make(store, [FakeResponse(200, {"updates": [], "marker": 998877})])
    r.poll_once()
    assert store.get_setting("max_marker") == "998877"

    session = FakeSession([FakeResponse(200, {"updates": [], "marker": 998878})])
    again = MaxReceiver(token="max-token", store=store, session=session)
    again.poll_once()
    assert session.calls[0]["params"]["marker"] == 998877


def test_first_poll_goes_without_marker(store):
    session = FakeSession([FakeResponse(200, {"updates": [], "marker": 1})])
    MaxReceiver(token="max-token", store=store, session=session).poll_once()
    assert "marker" not in session.calls[0]["params"]


def test_authorization_header_and_certificate_bundle(store):
    session = FakeSession([FakeResponse(200, {"updates": [], "marker": 1})])
    MaxReceiver(token="max-token", store=store, session=session).poll_once()
    call = session.calls[0]
    assert call["headers"]["Authorization"] == "max-token"
    assert call["verify"] == CA_BUNDLE
    assert call["url"].startswith("https://platform-api2.max.ru/updates")


def test_401_means_token_rejected(store):
    r = make(store, [FakeResponse(401, {"code": "verify.token", "message": "invalid token"})])
    with pytest.raises(TokenRejected):
        r.poll_once()


def test_429_is_rate_limited_with_retry_after_header(store):
    r = make(store, [FakeResponse(429, {"message": "too many requests"},
                                  headers={"Retry-After": "12"})])
    with pytest.raises(RateLimited) as exc:
        r.poll_once()
    assert exc.value.retry_after == 12


def test_405_is_a_conflict_the_loop_stops(store):
    """405 на /updates у Max значит, что у бота живёт вебхук-подписка:
    длинный опрос ему больше не отдают — крутить цикл бессмысленно."""
    r = make(store, [FakeResponse(405, {"message": "method not allowed"})])
    with pytest.raises(BridgeConflict):
        r.poll_once()


def test_non_json_answer_is_skipped_without_moving_marker(store):
    store.set_setting("max_marker", "5")
    r = make(store, [FakeResponse(502, payload=None, text="<html>502</html>")])
    assert r.poll_once() == []
    assert store.get_setting("max_marker") == "5"


def test_updates_of_other_types_are_ignored(store):
    r = make(store, [FakeResponse(200, {"updates": [{"update_type": "bot_started",
                                                     "chat_id": 1, "user": {"user_id": 2}}],
                                        "marker": 3})])
    assert r.poll_once() == []
    assert store.get_setting("max_marker") == "3"


def test_send_uses_chat_id_query_and_max_limit(store):
    session = FakeSession([FakeResponse(200, {"message": {}}), FakeResponse(200, {"message": {}})])
    MaxReceiver(token="max-token", store=store, session=session).send(900, "я" * 4500)
    assert len(session.calls) == 2
    for call in session.calls:
        assert call["params"]["chat_id"] == 900
        assert len(call["json"]["text"]) <= 4000


def test_token_is_never_printed_in_an_error(store):
    r = make(store, [FakeResponse(401, {"message": "token max-token is bad"})])
    with pytest.raises(TokenRejected) as exc:
        r.poll_once()
    assert "max-token" not in str(exc.value)


def test_long_answer_is_sent_at_the_pace_max_allows(store):
    """Max принимает не больше двух сообщений в секунду в один чат:
    длинный ответ шлём с паузой, иначе хвост потеряется на 429."""
    session = FakeSession([FakeResponse(200, {"message": {}})] * 3)
    slept = []
    r = MaxReceiver(token="max-token", store=store, session=session, sleeper=slept.append)
    r.send(900, "я" * 9000)
    assert len(session.calls) == 3
    assert len(slept) == 2                    # пауза между кусками, не после последнего
    assert all(s >= 0.5 for s in slept)


# --- этап 3: файлы туда и обратно -------------------------------------------

def with_file(name="отчёт за сентябрь.xlsx", size=2048, text=""):
    update = nested_update(text)
    update["message"]["body"]["attachments"] = [{
        "type": "file",
        "payload": {"url": "https://fu.oneme.ru/get/abc", "token": "file-token"},
        "filename": name, "size": size,
    }]
    return update


def test_file_attachment_becomes_our_attachment(store):
    r = make(store, [FakeResponse(200, {"updates": [with_file()], "marker": 1})])
    msg = r.poll_once()[0]
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.kind == "file"
    assert att.file_name == "отчёт за сентябрь.xlsx"
    assert att.size == 2048
    assert att.url == "https://fu.oneme.ru/get/abc"
    assert att.file_id == "file-token"


def test_caption_in_max_becomes_the_task(store):
    r = make(store, [FakeResponse(200, {"updates": [with_file(text="посчитай итог")],
                                        "marker": 1})])
    msg = r.poll_once()[0]
    assert msg.text == "посчитай итог"
    assert msg.attachments


def test_image_video_audio_are_attachments(store):
    update = nested_update("")
    update["message"]["body"]["attachments"] = [
        {"type": "image", "payload": {"url": "https://iu.oneme.ru/1", "token": "t1",
                                      "photo_id": 7}},
        {"type": "video", "payload": {"url": "https://v/2", "token": "t2"}},
        {"type": "audio", "payload": {"url": "https://a/3", "token": "t3"}},
    ]
    r = make(store, [FakeResponse(200, {"updates": [update], "marker": 1})])
    kinds = [a.kind for a in r.poll_once()[0].attachments]
    assert kinds == ["photo", "video", "audio"]


def test_sticker_and_location_are_not_files(store):
    update = nested_update("")
    update["message"]["body"]["attachments"] = [
        {"type": "sticker", "payload": {"code": "s"}},
        {"type": "location", "latitude": 1, "longitude": 2},
    ]
    r = make(store, [FakeResponse(200, {"updates": [update], "marker": 1})])
    assert r.poll_once() == []          # сказать нечего и файла нет


def test_file_is_downloaded_by_its_own_link(store):
    session = FakeSession([FakeResponse(200, content=b"body")])
    r = MaxReceiver(token="max-token", store=store, session=session)
    data = r.fetch(Attachment(kind="file", file_id="t", url="https://fu.oneme.ru/get/abc",
                              size=10))
    assert data == b"body"
    assert session.calls[0]["url"] == "https://fu.oneme.ru/get/abc"


def test_download_retries_with_the_token_when_the_link_asks_for_it(store):
    session = FakeSession([FakeResponse(401, {"code": "verify.token"}),
                           FakeResponse(200, content=b"body")])
    r = MaxReceiver(token="max-token", store=store, session=session)
    assert r.fetch(Attachment(kind="file", file_id="t", url="https://fu.oneme.ru/x")) == b"body"
    assert "Authorization" not in (session.calls[0].get("headers") or {})
    assert session.calls[1]["headers"]["Authorization"] == "max-token"


def test_file_without_a_link_is_an_honest_refusal(store):
    r = MaxReceiver(token="max-token", store=store, session=FakeSession([]))
    with pytest.raises(RuntimeError):
        r.fetch(Attachment(kind="file", file_id="t", url=""))


def test_too_big_incoming_file_is_refused_before_the_request(store):
    session = FakeSession([])
    r = MaxReceiver(token="max-token", store=store, session=session)
    with pytest.raises(FileTooBig):
        r.fetch(Attachment(kind="file", url="https://x", size=r.download_limit + 1))
    assert session.calls == []


def upload_responses(message_answers=None):
    return [
        FakeResponse(200, {"url": "https://fu.oneme.ru/upload/xyz"}),      # POST /uploads
        FakeResponse(200, {"token": "uploaded-token"}),                    # multipart
    ] + list(message_answers or [FakeResponse(200, {"message": {"body": {"mid": "m"}}})])


def test_file_goes_out_in_three_steps(store, tmp_path):
    path = tmp_path / "отчёт за сентябрь.xlsx"
    path.write_bytes(b"data")
    session = FakeSession(upload_responses())
    r = MaxReceiver(token="max-token", store=store, session=session, sleeper=lambda s: None)
    r.send_file(900, path, caption="вот он")

    first, second, third = session.calls
    assert first["url"].endswith("/uploads")
    assert first["params"]["type"] == "file"
    assert first["headers"]["Authorization"] == "max-token"

    assert second["url"] == "https://fu.oneme.ru/upload/xyz"
    assert second["files"]["data"][0] == "отчёт за сентябрь.xlsx"
    assert "Authorization" not in (second.get("headers") or {})

    assert third["url"].endswith("/messages")
    assert third["params"]["chat_id"] == 900
    assert third["json"]["attachments"] == [{"type": "file",
                                             "payload": {"token": "uploaded-token"}}]
    assert third["json"]["text"] == "вот он"


def test_token_from_the_first_step_is_used_when_the_upload_is_silent(store, tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    session = FakeSession([
        FakeResponse(200, {"url": "https://fu.oneme.ru/upload/xyz", "token": "early-token"}),
        FakeResponse(200, {}),
        FakeResponse(200, {"message": {}}),
    ])
    r = MaxReceiver(token="max-token", store=store, session=session, sleeper=lambda s: None)
    r.send_file(900, path)
    assert session.calls[2]["json"]["attachments"][0]["payload"]["token"] == "early-token"


def test_not_ready_file_is_sent_again_after_a_pause(store, tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    waited = []
    session = FakeSession(upload_responses([
        FakeResponse(400, {"code": "attachment.not.ready", "message": "not processed"}),
        FakeResponse(400, {"code": "attachment.not.ready", "message": "not processed"}),
        FakeResponse(200, {"message": {"body": {"mid": "m"}}}),
    ]))
    r = MaxReceiver(token="max-token", store=store, session=session,
                    sleeper=lambda seconds: waited.append(seconds))
    r.send_file(900, path)
    assert len([c for c in session.calls if c["url"].endswith("/messages")]) == 3
    assert waited and waited[-1] > waited[0]      # пауза растёт, как просит их документация


def test_file_that_never_gets_ready_is_an_honest_error(store, tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    answers = [FakeResponse(400, {"code": "attachment.not.ready"})] * 20
    session = FakeSession(upload_responses(answers))
    r = MaxReceiver(token="max-token", store=store, session=session, sleeper=lambda s: None)
    with pytest.raises(RuntimeError) as exc:
        r.send_file(900, path)
    assert "max-token" not in str(exc.value)


def test_upload_trouble_never_shows_the_token(store, tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    session = FakeSession([FakeResponse(401, {"code": "verify.token",
                                              "message": "max-token bad"})])
    r = MaxReceiver(token="max-token", store=store, session=session, sleeper=lambda s: None)
    with pytest.raises(RuntimeError) as exc:
        r.send_file(900, path)
    assert "max-token" not in str(exc.value)


# --- этап 4: голос ----------------------------------------------------------

def test_audio_attachment_carries_its_length(store):
    update = nested_update("")
    update["message"]["body"]["attachments"] = [
        {"type": "audio", "payload": {"url": "https://a/3", "token": "t3"},
         "duration": 25},
    ]
    r = make(store, [FakeResponse(200, {"updates": [update], "marker": 1})])
    att = r.poll_once()[0].attachments[0]
    assert att.kind == "audio"
    assert att.duration == 25


def test_voice_answer_goes_out_through_the_audio_upload(store, tmp_path):
    path = tmp_path / "otvet.ogg"
    path.write_bytes(b"OggS")
    session = FakeSession(upload_responses())
    r = MaxReceiver(token="max-token", store=store, session=session, sleeper=lambda s: None)
    r.send_voice(900, path, caption="Готово")

    first, second, third = session.calls
    assert first["url"].endswith("/uploads")
    assert first["params"]["type"] == "audio"        # не file: иначе это не запись
    assert second["url"] == "https://fu.oneme.ru/upload/xyz"
    assert third["json"]["attachments"] == [{"type": "audio",
                                             "payload": {"token": "uploaded-token"}}]
    assert third["json"]["text"] == "Готово"


def test_too_big_voice_answer_is_refused_in_max(store, tmp_path):
    path = tmp_path / "otvet.ogg"
    path.write_bytes(b"x")
    r = MaxReceiver(token="max-token", store=store, session=FakeSession([]))
    r.upload_limit = 0
    with pytest.raises(FileTooBig):
        r.send_voice(900, path)


def test_the_name_of_the_sender_comes_along(store):
    session = FakeSession([FakeResponse(200, {"updates": [
        {"update_type": "message_created", "timestamp": 1,
         "message": {"sender": {"user_id": 777, "name": "Наталья"},
                     "recipient": {"chat_id": 10},
                     "body": {"text": "привет"}}}], "marker": 5})])
    got = MaxReceiver(token="t", store=store, session=session).poll_once()
    assert got[0].name == "Наталья"


# --- вид файла в multipart (живая приёмка 15.09) ----------------------------

def test_upload_names_the_kind_of_file(store, tmp_path):
    """Max отказывает загрузке, у которой в multipart нет вида файла.

    Живьём 15.09: без `Content-Type` у части формы их файловый узел отвечает
    403 «There is no file in request» с кодом `upload.error` — файл он в запросе
    просто не видит. Имя из трёх частей (имя, тело, вид) лечит это.
    """
    path = tmp_path / "отчёт-сентябрь.md"
    path.write_bytes(b"data")
    session = FakeSession(upload_responses())
    r = MaxReceiver(token="max-token", store=store, session=session, sleeper=lambda s: None)
    r.send_file(900, path)

    part = session.calls[1]["files"]["data"]
    assert len(part) == 3, "у части формы должен быть третий член — вид файла"
    assert part[0] == "отчёт-сентябрь.md"
    assert part[2] == "text/markdown"


def test_unknown_extension_still_gets_a_kind(store, tmp_path):
    """Вид не угадался — говорим «просто байты», а не молчим."""
    path = tmp_path / "выгрузка.огурец"
    path.write_bytes(b"data")
    session = FakeSession(upload_responses())
    r = MaxReceiver(token="max-token", store=store, session=session, sleeper=lambda s: None)
    r.send_file(900, path)

    assert session.calls[1]["files"]["data"][2] == "application/octet-stream"
