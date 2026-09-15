"""Приёмник Max: своя схема обновлений (marker, а не update_id), те же три класса ошибок."""
import pytest

from bridge.receivers.base import BridgeConflict, TokenRejected, RateLimited
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
