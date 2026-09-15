"""Приёмник Telegram: long polling, персистентный offset, три класса ошибок."""
import pytest

from bridge.receivers.base import BridgeConflict, TokenRejected, RateLimited
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
    r = make(store, [FakeResponse(200, {"ok": True, "result": [
        {"update_id": 3, "message": {"from": {"id": 1}, "chat": {"id": 2},
                                     "voice": {"file_id": "x"}}}]})])
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
