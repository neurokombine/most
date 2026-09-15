"""Цикл демона: два канала в одном процессе, три класса ошибок по поведению."""
import pytest
import requests

from bridge.daemon import Bridge, EXIT_STALE
from bridge.executor import FakeExecutor
from bridge.receivers.base import BridgeConflict, Incoming, RateLimited, TokenRejected


class FakeReceiver:
    def __init__(self, channel, batches=None, limit=4096):
        self.channel = channel
        self.limit = limit
        self.batches = list(batches or [])
        self.sent = []
        self.polls = 0

    def poll_once(self):
        self.polls += 1
        if not self.batches:
            return []
        nxt = self.batches.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))


def msg(channel, text="посчитай", user_id=111, chat_id=500):
    return Incoming(channel=channel, chat_id=chat_id, user_id=user_id, text=text,
                    thread_id=0, raw={})


@pytest.fixture()
def bridge(config, store):
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    tg = FakeReceiver("telegram", [[msg("telegram")]])
    mx = FakeReceiver("max", [[msg("max", user_id=222, chat_id=900)]], limit=4000)
    b = Bridge(config=config, store=store, executor=FakeExecutor(text="Готово."),
               receivers={"telegram": tg, "max": mx}, sleeper=lambda s: None)
    return b


def test_tick_answers_into_the_channel_the_question_came_from(bridge):
    bridge.tick()
    tg = bridge.receivers["telegram"]
    mx = bridge.receivers["max"]
    assert tg.sent == [(500, "Готово.")]
    assert mx.sent == [(900, "Готово.")]


def test_stranger_gets_no_answer_at_all(config, store):
    store.sync_allowlist("telegram", [111])
    tg = FakeReceiver("telegram", [[msg("telegram", user_id=999)]])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.tick()
    assert tg.sent == []
    assert store.recent_strangers()[0]["user_id"] == 999


def test_conflict_stops_only_its_own_channel(config, store):
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    tg = FakeReceiver("telegram", [BridgeConflict("вебхук")])
    mx = FakeReceiver("max", [[msg("max", user_id=222, chat_id=900)]])
    b = Bridge(config=config, store=store, executor=FakeExecutor(text="Готово."),
               receivers={"telegram": tg, "max": mx}, sleeper=lambda s: None)
    b.tick()
    assert "telegram" not in b.receivers
    assert mx.sent == [(900, "Готово.")]
    assert b.alive() is True


def test_token_rejected_stops_its_channel_and_is_written_down(config, store):
    tg = FakeReceiver("telegram", [TokenRejected("401")])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.tick()
    assert b.alive() is False
    kinds = [row["kind"] for row in store.recent_journal()]
    assert "stopped" in kinds
    assert any("токен" in (row["text"] or "").lower() for row in store.recent_journal())


def test_rate_limit_waits_exactly_as_asked(config, store):
    slept = []
    tg = FakeReceiver("telegram", [RateLimited("подожди", retry_after=17), []])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=slept.append)
    b.tick()
    assert slept == [17]
    assert "telegram" in b.receivers


def test_network_trouble_is_survived(config, store):
    slept = []
    tg = FakeReceiver("telegram", [requests.exceptions.ConnectionError("нет сети"), []])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=slept.append)
    b.tick()
    assert slept and slept[0] >= 5
    assert "telegram" in b.receivers


def test_one_broken_message_does_not_stop_the_tick(config, store):
    store.sync_allowlist("telegram", [111])

    class Boom(FakeExecutor):
        def run(self, prompt, workdir, session_id=None):
            raise RuntimeError("внутри всё сломалось")

    tg = FakeReceiver("telegram", [[msg("telegram"), msg("telegram", text="вторая")]])
    b = Bridge(config=config, store=store, executor=Boom(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.tick()
    assert tg.polls == 1
    assert len(tg.sent) == 2                       # обеим ответили по-человечески
    assert "Traceback" not in tg.sent[0][1]


def test_code_change_asks_for_a_restart(config, store, monkeypatch):
    tg = FakeReceiver("telegram", [[], []])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    monkeypatch.setattr(b, "code_changed", lambda: True)
    assert b.run(max_ticks=3) == EXIT_STALE


def test_loop_ends_quietly_when_no_channel_is_left(config, store):
    tg = FakeReceiver("telegram", [TokenRejected("401")])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    assert b.run(max_ticks=5) == 0
    assert tg.polls == 1
