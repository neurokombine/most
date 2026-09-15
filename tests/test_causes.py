"""Три причины молчания — ворота 2 спецификации: каждая воспроизводима командой.

Урок Б.6 держится на одном кадре: ученик пишет боту, бот молчит, ученик
говорит нейросети «покажи, кто стучался, и пускай только меня» — и бот
заговорил. Здесь эти причины воспроизводятся тестом: каждую видно журналом
или доктором, и на каждую есть человеческий текст, а не номер кода.

  а · человека нет в списке своих   → видно в `knock`, лечится `allow last`
  б · того же бота слушает второй   → канал гаснет, доктор находит соседа
  в · токен не признан              → канал гаснет, сказано, что перевыпустить
  г · мост не запущен вовсе         → `status` говорит это первой строкой
"""
import os

from bridge import cli, doctor, lock, texts
from bridge.daemon import Bridge
from bridge.executor import FakeExecutor
from bridge.receivers.base import BridgeConflict, Incoming, TokenRejected
from tests.test_daemon import FakeReceiver


def msg(channel="telegram", user_id=555, text="привет", name="Наталья"):
    return Incoming(channel=channel, chat_id=500, user_id=user_id, text=text,
                    thread_id=0, name=name, raw={})


def bridge_with(config, store, batches, channel="telegram"):
    receiver = FakeReceiver(channel, batches)
    b = Bridge(config=config, store=store, executor=FakeExecutor(text="Готово."),
               receivers={channel: receiver}, sleeper=lambda s: None)
    return b, receiver


# --- причина а: человека нет в списке своих ---------------------------------

def test_a_stranger_gets_silence_and_a_line_in_the_journal(config, store, capsys):
    config.telegram.allowlist = []
    config.max = None
    b, tg = bridge_with(config, store, [[msg()]])
    b.tick()
    assert tg.sent == []                                  # бот молчит, и это защита

    code = cli.run("knock", [], config=config, store=store)
    said = capsys.readouterr().out
    assert code == 0
    assert "555" in said and "Наталья" in said and "промолчала" in said


def test_one_phrase_lets_the_knocker_in_and_the_bot_speaks(config, store, capsys, home):
    """Главный кадр урока целиком: молчание → «пускай только меня» → ответ."""
    (home / "config.yaml").write_text('telegram:\n  token: "t"\n  allowlist: []\n',
                                      encoding="utf-8")
    os.chmod(home / "config.yaml", 0o600)
    config.telegram.allowlist = []
    config.max = None

    b, tg = bridge_with(config, store, [[msg()], [msg(text="посчитай")]])
    b.tick()
    assert tg.sent == []

    assert cli.run("allow", ["last"], config=config, store=store) == 0
    assert "перезапус" in capsys.readouterr().out.lower()

    b.tick()                                              # мост не перезапускали
    b.pool.wait_idle()
    b.deliver()
    assert tg.sent[-1] == (500, "Готово.")
    assert "allowlist: [555]" in (home / "config.yaml").read_text(encoding="utf-8")


# --- причина б: того же бота слушает кто-то ещё -----------------------------

def test_a_second_listener_puts_the_channel_out_with_human_words(config, store):
    config.max = None
    b, tg = bridge_with(config, store, [BridgeConflict("409 Conflict: terminated by other")])
    assert b.run(max_ticks=3) == 0                        # чинится руками, респавн не поможет

    said = " ".join(row["text"] or "" for row in store.recent_journal())
    assert "слушает" in said
    assert "409" not in said
    assert "Conflict" not in said


def test_the_doctor_finds_the_second_bridge_on_the_same_machine(config):
    lines = [f"  4242 /usr/bin/python3 -m bridge --name {config.name}"]
    check = doctor.check_twins(config, lines=lines, mine=999)
    assert check.ok is False
    assert "4242" in check.what
    assert "один бот — один слушатель" in (check.hint or "")


def test_a_second_bridge_of_the_same_name_does_not_start(home):
    first = lock.InstanceLock(home / "most.lock")
    assert first.acquire() is True
    try:
        assert lock.InstanceLock(home / "most.lock").acquire() is False
    finally:
        first.release()


# --- причина в: токен не признан --------------------------------------------

def test_a_rejected_token_is_explained_by_what_to_do(config, store):
    config.max = None
    b, tg = bridge_with(config, store, [TokenRejected("401 Unauthorized")])
    assert b.run(max_ticks=3) == 0

    said = " ".join(row["text"] or "" for row in store.recent_journal())
    assert "токен" in said.lower()
    assert "401" not in said
    assert "config.yaml" in said or "настройк" in said


def test_the_doctor_says_the_same_about_a_rejected_token(config):
    from tests.fakes import FakeResponse, FakeSession
    check = doctor.check_network("telegram", session=FakeSession([FakeResponse(401, {})]),
                                 token="abc:123")
    assert check.ok is False
    assert "401" not in check.what + (check.hint or "")
    assert "отц" in (check.hint or "").lower()            # к отцу ботов


# --- причина г: мост не запущен вовсе ---------------------------------------

def test_status_names_the_quiet_cause_first(config, store, capsys):
    code = cli.run("status", [], config=config, store=store)
    said = capsys.readouterr().out
    assert code == 1
    assert said.splitlines()[0].endswith("не запущен — поэтому бот и молчит.")
    assert "most@" in said


def test_every_cause_has_a_text_without_a_single_number_of_an_error():
    for name in ("TELEGRAM_CONFLICT", "MAX_CONFLICT", "TELEGRAM_TOKEN_REJECTED",
                 "MAX_TOKEN_REJECTED", "ALREADY_RUNNING"):
        text = getattr(texts, name)
        assert "409" not in text and "401" not in text and "404" not in text
        assert "exit" not in text.lower()
