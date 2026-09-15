"""Командная строка моста: этим нейросеть в серверном окне управляет мостом.

Ученик говорит словами («покажи, кто стучался, и пускай только меня»), а
нейросеть читает README и зовёт вот эти команды. Значит, их вывод должен
читаться и ею, и человеком: по-русски, без жаргона, плюс --json для разбора.
"""
import json
import os

import pytest

from bridge import cli, lock
from bridge.config import ChannelConfig
from tests.fakes import FakeResponse, FakeSession


@pytest.fixture()
def settings(config, home):
    (home / "config.yaml").write_text(
        'telegram:\n  token: "abc:123"\n  allowlist: []   # свои\n'
        'max:\n  token: "max-abc"\n  allowlist: []\n', encoding="utf-8")
    os.chmod(home / "config.yaml", 0o600)
    # Свежий мост: в настройках список своих пуст — как у ученика до белого списка.
    config.telegram = ChannelConfig(token="abc:123", allowlist=[])
    config.max = ChannelConfig(token="max-abc", allowlist=[])
    return config


def out(capsys):
    return capsys.readouterr().out


def run(command, args=(), *, config, store, capsys, **kw):
    code = cli.run(command, list(args), config=config, store=store, **kw)
    return code, out(capsys)


# --- кто стучался -----------------------------------------------------------

def test_knock_on_an_empty_journal_says_it_plainly(settings, store, capsys):
    code, text = run("knock", config=settings, store=store, capsys=capsys)
    assert code == 0
    assert "никто" in text.lower()


def test_knock_shows_time_channel_number_name_and_the_answer(settings, store, capsys):
    store.note_stranger("telegram", 500, 555, "привет", name="Наталья")
    code, text = run("knock", config=settings, store=store, capsys=capsys)
    assert code == 0
    assert "МСК" in text
    assert "телеграм" in text
    assert "555" in text
    assert "Наталья" in text
    assert "привет" in text
    assert "промолчала" in text
    assert "allow telegram:555" in text        # что сказать дальше — тут же


def test_knock_can_be_read_by_a_machine(settings, store, capsys):
    store.note_stranger("max", 500, 777, "привет", name="Наталья")
    code, text = run("knock", config=settings, store=store, capsys=capsys, as_json=True)
    data = json.loads(text)
    assert code == 0
    assert data["knocks"][0]["user_id"] == 777
    assert data["knocks"][0]["channel"] == "max"
    assert data["knocks"][0]["answer"] == "промолчала"
    assert data["knocks"][0]["allowed"] is False


def test_knock_marks_those_who_are_already_let_in(settings, store, capsys):
    store.note_stranger("telegram", 500, 555, "привет")
    store.allow("telegram", 555)
    code, text = run("knock", config=settings, store=store, capsys=capsys)
    assert "уже" in text.lower()


# --- пустить и не пускать ---------------------------------------------------

def test_allow_writes_both_into_the_settings_and_into_the_base(settings, store, capsys):
    code, text = run("allow", ["telegram:555"], config=settings, store=store, capsys=capsys)
    assert code == 0
    assert store.is_allowed("telegram", 555) is True
    assert "555" in text
    assert "перезапус" in text.lower()          # перезапускать мост не надо
    assert "allowlist: [555]" in (settings.home / "config.yaml").read_text(encoding="utf-8")


def test_allow_last_lets_in_the_last_knocker_of_every_channel(settings, store, capsys):
    store.note_stranger("telegram", 500, 111, "привет")
    store.note_stranger("telegram", 500, 555, "привет", name="Наталья")
    store.note_stranger("max", 900, 777, "привет", name="Наталья")
    code, text = run("allow", ["last"], config=settings, store=store, capsys=capsys)
    assert code == 0
    assert store.is_allowed("telegram", 555) is True
    assert store.is_allowed("max", 777) is True
    assert store.is_allowed("telegram", 111) is False      # это был не последний
    assert "Наталья" in text


def test_allow_last_without_a_single_knock_is_honest(settings, store, capsys):
    code, text = run("allow", ["last"], config=settings, store=store, capsys=capsys)
    assert code == 1
    assert "никто" in text.lower()


def test_allow_needs_to_know_the_messenger_when_there_are_two(settings, store, capsys):
    code, text = run("allow", ["555"], config=settings, store=store, capsys=capsys)
    assert code == 1
    assert "telegram:555" in text and "max:555" in text


def test_allow_takes_a_bare_number_when_there_is_one_messenger(settings, store, capsys):
    settings.max = None
    code, text = run("allow", ["555"], config=settings, store=store, capsys=capsys)
    assert code == 0
    assert store.is_allowed("telegram", 555) is True


def test_allow_says_plainly_that_the_person_is_already_in(settings, store, capsys):
    run("allow", ["telegram:555"], config=settings, store=store, capsys=capsys)
    code, text = run("allow", ["telegram:555"], config=settings, store=store, capsys=capsys)
    assert code == 0
    assert "уже" in text.lower()


def test_deny_takes_the_person_out_of_both_places(settings, store, capsys):
    run("allow", ["telegram:555"], config=settings, store=store, capsys=capsys)
    code, text = run("deny", ["telegram:555"], config=settings, store=store, capsys=capsys)
    assert code == 0
    assert store.is_allowed("telegram", 555) is False
    assert "allowlist: []" in (settings.home / "config.yaml").read_text(encoding="utf-8")


def test_a_broken_argument_is_explained(settings, store, capsys):
    code, text = run("allow", ["телеграм:Наталья"], config=settings, store=store, capsys=capsys)
    assert code == 1
    assert "allow telegram:" in text


# --- кто свои ---------------------------------------------------------------

def test_who_lists_the_allowed_with_channels(settings, store, capsys):
    store.allow("telegram", 555, note="Наталья")
    code, text = run("who", config=settings, store=store, capsys=capsys)
    assert code == 0
    assert "555" in text and "телеграм" in text


def test_who_on_an_empty_list_warns_that_the_bridge_answers_nobody(settings, store, capsys):
    code, text = run("who", config=settings, store=store, capsys=capsys)
    assert code == 0
    assert "никому" in text.lower()


# --- жив ли мост ------------------------------------------------------------

def test_status_says_the_bridge_is_not_running_and_how_to_start_it(settings, store, capsys):
    code, text = run("status", config=settings, store=store, capsys=capsys)
    assert code == 1
    assert "не запущен" in text
    assert "-m bridge --name" in text


def test_status_sees_a_running_bridge(settings, store, capsys):
    held = lock.InstanceLock(settings.home / "most.lock")
    assert held.acquire()
    try:
        code, text = run("status", config=settings, store=store, capsys=capsys)
    finally:
        held.release()
    assert code == 0
    assert "запущен" in text
    assert str(os.getpid()) in text


def test_status_tells_when_the_messengers_were_last_heard(settings, store, capsys):
    from bridge.store import now_iso
    store.set_setting("heard:telegram", now_iso())
    code, text = run("status", config=settings, store=store, capsys=capsys)
    assert "МСК" in text
    assert "Max" in text                      # про второй канал тоже сказано


def test_status_shows_the_last_works_and_the_schedule(settings, store, capsys):
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    job = store.start_job(link["id"], "telegram", 500, "s", "собери сводку", "")
    store.finish_job(job, "done", exit_code=0, duration_sec=12.0, result_head="готово")
    store.add_schedule(link["id"], "каждый день в 07:30", "собирай сводку",
                       project="buhgalter")
    code, text = run("status", config=settings, store=store, capsys=capsys)
    assert "собери сводку" in text
    assert "07:30" in text


def test_status_can_be_read_by_a_machine(settings, store, capsys):
    code, text = run("status", config=settings, store=store, capsys=capsys, as_json=True)
    data = json.loads(text)
    assert data["running"] is False
    assert "telegram" in data["heard"]


# --- доктор и «скажи в чат» -------------------------------------------------

def test_doctor_prints_a_table_and_returns_one_when_something_is_wrong(settings, store,
                                                                      capsys):
    session = FakeSession([FakeResponse(200, {"ok": True}), FakeResponse(200, {"ok": True})])
    code, text = run("doctor", config=settings, store=store, capsys=capsys,
                     session=session, claude_bin="/nen/sushchestvuet/claude")
    assert "В порядке" in text or "Не в порядке" in text
    assert code == 1                                  # claude нет — доктор честен


def test_say_sends_the_text_into_every_channel(settings, store, capsys):
    store.upsert_link("telegram", 500, 0)
    sent = []

    class FakeReceiver:
        channel = "telegram"
        limit = 4096

        def send(self, chat_id, text):
            sent.append((chat_id, text))

    code, text = run("say", ["проверка связи"], config=settings, store=store,
                     capsys=capsys, receivers={"telegram": FakeReceiver()})
    assert code == 0
    assert sent == [(500, "проверка связи")]
    assert "телеграм" in text


def test_say_without_a_single_chat_is_honest(settings, store, capsys):
    class FakeReceiver:
        channel = "telegram"
        limit = 4096

        def send(self, chat_id, text):
            raise AssertionError("отправлять некуда")

    code, text = run("say", ["привет"], config=settings, store=store, capsys=capsys,
                     receivers={"telegram": FakeReceiver()})
    assert code == 1
    assert "не с кем" in text or "ни одного чата" in text


def test_an_unknown_command_is_answered_with_the_list_of_known_ones(settings, store, capsys):
    code, text = run("сделай-хорошо", config=settings, store=store, capsys=capsys)
    assert code == 1
    assert "knock" in text and "status" in text


def test_status_shows_the_last_troubles_so_the_silence_has_a_reason(settings, store, capsys):
    """Канал погас — об этом написано в журнале, и `status` обязан это показать."""
    store.note("stopped", channel="telegram",
               text="Этого бота уже слушает кто-то ещё: второй мост")
    code, text = run("status", config=settings, store=store, capsys=capsys)
    assert "слушает кто-то ещё" in text
    assert "телеграм" in text
