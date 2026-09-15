"""Доктор: заготовка этапа 6 — токен и сеть. Сети в тестах нет."""
from bridge.doctor import (Check, check_claude, check_config, check_network,
                           check_voice, checkup)
from tests.fakes import FakeResponse, FakeSession


def test_check_is_told_in_russian(config):
    checks = checkup(config, session=FakeSession([FakeResponse(200, {"ok": True})]),
                     claude_bin="/nen/sushchestvuet/claude")
    assert checks
    for check in checks:
        assert check.what
        assert check.what == check.what.strip()
        assert not check.what.isupper()


def test_config_check_notices_loose_permissions(config, home):
    (home / "config.yaml").write_text("telegram:\n  token: x\n", encoding="utf-8")
    import os
    os.chmod(home / "config.yaml", 0o644)
    check = check_config(config)
    assert check.ok is False
    assert "chmod" in (check.hint or "")


def test_claude_check_fails_when_the_binary_is_missing():
    check = check_claude("/nen/sushchestvuet/claude")
    assert check.ok is False
    assert "claude" in check.what.lower() or "claude" in (check.hint or "").lower()


def test_network_check_is_ok_on_200(config):
    check = check_network("telegram", session=FakeSession([FakeResponse(200, {"ok": True})]),
                          token="abc:123")
    assert check.ok is True


def test_network_check_is_honest_on_401(config):
    check = check_network("telegram", session=FakeSession([FakeResponse(401, {"ok": False})]),
                          token="abc:123")
    assert check.ok is False
    assert "токен" in (check.what + (check.hint or "")).lower()


def test_network_check_survives_a_dead_network(config):
    import requests
    session = FakeSession([requests.exceptions.ConnectionError("нет сети")])
    check = check_network("max", session=session, token="max-abc")
    assert check.ok is False
    assert "сет" in (check.what + (check.hint or "")).lower()


def test_token_is_never_shown(config):
    check = check_network("telegram", session=FakeSession([FakeResponse(401, {"ok": False})]),
                          token="super-secret")
    assert "super-secret" not in check.what + (check.hint or "")


def test_empty_token_is_named_plainly_without_touching_the_network():
    from tests.fakes import FakeSession as _FS
    session = _FS([])
    check = check_network("telegram", session=session, token="")
    assert check.ok is False
    assert "не вписан" in check.what
    assert session.calls == []          # в сеть за этим ходить незачем


# --- этап 4: голос ----------------------------------------------------------

def test_voice_check_says_it_is_off_when_switched_off(config):
    config.voice.enabled = False
    check = check_voice(config)
    assert check.ok is True
    assert "выключ" in check.what


def test_voice_check_is_calm_without_the_library(config, tmp_path):
    """Библиотеки нет — это не поломка: мост работает текстом и так и говорит."""
    config.voice.model_dir = tmp_path / "models"
    check = check_voice(config, library=False)
    assert check.ok is True
    assert "текстом" in check.what or "текстом" in (check.hint or "")


def test_voice_check_catches_a_missing_model(config, tmp_path):
    config.voice.model_dir = tmp_path / "models"
    check = check_voice(config, library=True)
    assert check.ok is False
    assert "--voice" in (check.hint or "")


def test_voice_check_is_happy_when_the_model_is_on_disk(config, tmp_path):
    folder = tmp_path / "models" / "models--Systran--faster-whisper-small" / "snapshots" / "a"
    folder.mkdir(parents=True)
    (folder / "model.bin").write_bytes(b"x")
    config.voice.model_dir = tmp_path / "models"
    check = check_voice(config, library=True)
    assert check.ok is True
    assert "small" in check.what


def test_checkup_includes_the_voice(config):
    checks = checkup(config, session=FakeSession([FakeResponse(200, {"ok": True})]),
                     claude_bin="/nen/sushchestvuet/claude")
    assert any("голос" in c.what.lower() for c in checks)


# --- этап 6: замок, второй мост, часы, память, диск, живой вход --------------

import os          # noqa: E402
from datetime import datetime, timedelta, timezone   # noqa: E402

from bridge import lock      # noqa: E402
from bridge.doctor import (check_clock, check_disk, check_live, check_lock,
                           check_memory, check_twins, table, verdict)   # noqa: E402


def test_the_doctor_sees_a_running_bridge(config):
    held = lock.InstanceLock(config.home / "most.lock")
    assert held.acquire()
    try:
        check = check_lock(config)
    finally:
        held.release()
    assert check.ok is True
    assert str(os.getpid()) in check.what


def test_a_bridge_that_is_not_running_is_named_but_not_called_broken(config):
    """Доктора зовут и до первого запуска — это не поломка, а состояние."""
    check = check_lock(config)
    assert check.ok is True
    assert "не запущен" in check.what
    assert "most@" in (check.hint or "")


def test_the_second_bridge_of_the_same_name_is_a_trouble(config):
    lines = ["  501 /usr/bin/python3 -m bridge --name test",
             "  777 /usr/bin/python3 -m bridge --name boris"]
    check = check_twins(config, lines=lines, mine=999)
    assert check.ok is False
    assert "501" in check.what
    assert "слушает" in check.what or "слушател" in (check.hint or "")


def test_a_lonely_bridge_has_no_twins(config):
    check = check_twins(config, lines=["  777 /usr/bin/python3 -m bridge --name boris"],
                        mine=999)
    assert check.ok is True


def test_the_clock_shows_both_times(config):
    check = check_clock(config)
    assert check.ok is True
    assert "МСК" in check.what


def test_a_machine_living_in_another_zone_is_named_without_panic(config):
    machine = datetime.now(timezone(timedelta(hours=-4)))
    check = check_clock(config, machine=machine)
    assert check.ok is True
    assert "МСК" in check.what
    assert check.hint                      # сказано, что мост считает по Москве


def test_a_machine_whose_clock_has_run_away_is_a_trouble(config):
    machine = datetime.now(timezone.utc) + timedelta(minutes=30)
    check = check_clock(config, machine=machine)
    assert check.ok is False
    assert "час" in check.what.lower() or "минут" in check.what.lower()


def test_little_memory_is_a_warning_about_voice_and_parallel(config):
    check = check_memory(free=900 * 1024 * 1024)
    assert check.ok is False
    assert "голос" in (check.what + (check.hint or "")).lower()
    assert "parallel" in (check.hint or "")


def test_enough_memory_is_fine(config):
    check = check_memory(free=3 * 1024 * 1024 * 1024)
    assert check.ok is True
    assert "ГБ" in check.what


def test_memory_that_cannot_be_measured_does_not_break_the_doctor(config):
    check = check_memory(free=None)
    assert check.ok is True
    assert "не смогла" in check.what or "не измерила" in check.what


def test_a_full_disk_is_a_trouble(config):
    check = check_disk(config, free=200 * 1024 * 1024)
    assert check.ok is False
    assert "мест" in check.what.lower()


def test_a_disk_with_room_is_fine(config):
    check = check_disk(config, free=20 * 1024 * 1024 * 1024)
    assert check.ok is True


def test_the_live_check_is_not_run_without_being_asked(config):
    checks = checkup(config, session=FakeSession([FakeResponse(200, {"ok": True}),
                                                  FakeResponse(200, {"ok": True})]),
                     claude_bin="/nen/sushchestvuet/claude")
    assert not any("подписк" in c.what.lower() for c in checks)


def test_the_live_check_asks_the_neural_net_and_believes_the_answer(config):
    said = []

    def runner(cmd, **kw):
        said.append(cmd)

        class Done:
            returncode = 0
            stdout = "ок"
            stderr = ""
        return Done()

    check = check_live(config, runner=runner, claude_bin="/bin/claude")
    assert check.ok is True
    assert any("-p" in str(c) for c in said)
    assert "подписк" in check.what.lower() or "вход" in check.what.lower()


def test_a_dead_subscription_is_explained_without_a_single_number(config):
    def runner(cmd, **kw):
        class Done:
            returncode = 1
            stdout = ""
            stderr = "Invalid API key · Please run /login"
        return Done()

    check = check_live(config, runner=runner, claude_bin="/bin/claude")
    assert check.ok is False
    assert "claude" in (check.hint or "").lower()


def test_the_table_and_the_verdict_are_readable(config):
    checks = checkup(config, session=FakeSession([FakeResponse(200, {"ok": True}),
                                                  FakeResponse(200, {"ok": True})]),
                     claude_bin="/nen/sushchestvuet/claude")
    lines = table(checks)
    assert all(isinstance(line, str) for line in lines)
    assert any(line.startswith("В порядке") or line.startswith("Не в порядке")
               for line in lines)
    assert "Не в порядке" in verdict(checks, config)


def test_the_full_checkup_looks_at_the_machine_too(config):
    checks = checkup(config, session=FakeSession([FakeResponse(200, {"ok": True}),
                                                  FakeResponse(200, {"ok": True})]),
                     claude_bin="/nen/sushchestvuet/claude")
    said = " ".join(c.what.lower() for c in checks)
    assert "памят" in said
    assert "мест" in said or "диск" in said
    assert "мск" in said


class FakeRun:
    """Подставной `subprocess.run`: отвечает по первому доводу команды."""

    def __init__(self, version=(0, "2.1.267 (Claude Code)"), auth=(0, '{"loggedIn": true}')):
        self.version, self.auth = version, auth
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        code, out = self.auth if "auth" in cmd else self.version

        class Done:
            returncode, stdout, stderr = code, out, ""
        return Done()


def test_claude_check_notices_that_the_login_has_expired(tmp_path):
    """`claude --version` отвечает и без входа — а работа при этом не пойдёт.

    Код возврата у этой команды без входа — единица, и это не «не смогла
    спросить»: ответ разборчив и говорит прямо. Замерено на сервере 15.09.
    """
    binary = tmp_path / "claude"
    binary.write_text("", encoding="utf-8")
    runner = FakeRun(auth=(1, '{"loggedIn": false, "authMethod": "none"}'))
    check = check_claude(str(binary), runner=runner)
    assert check.ok is False
    assert "вход" in check.what.lower()
    assert "login" in (check.hint or "")


def test_claude_check_is_green_when_the_login_is_alive(tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("", encoding="utf-8")
    check = check_claude(str(binary), runner=FakeRun())
    assert check.ok is True
    assert "2.1.267" in check.what


def test_claude_check_does_not_scare_when_it_cannot_ask_about_the_login(tmp_path):
    """Старая версия без такой команды — не повод пугать человека."""
    binary = tmp_path / "claude"
    binary.write_text("", encoding="utf-8")
    check = check_claude(str(binary), runner=FakeRun(auth=(1, "unknown command auth")))
    assert check.ok is True


def test_the_free_question_about_the_login_does_not_wake_the_model(tmp_path):
    """Спрашиваем сохранённый вход, а не модель: подписка на это не тратится."""
    binary = tmp_path / "claude"
    binary.write_text("", encoding="utf-8")
    runner = FakeRun()
    check_claude(str(binary), runner=runner)
    assert ["auth", "status"] == runner.calls[-1][-2:]
    assert all("-p" not in call for call in runner.calls)


def test_the_running_bridge_is_not_its_own_twin(config, home):
    """Доктор советовал убить тот самый мост, который работает. Найдено на сервере."""
    from bridge.doctor import check_twins

    rows = [f"5539 /home/u/most/.venv/bin/python -m bridge --name {config.name}"]
    beda = check_twins(config, lines=rows, mine=9999, holder=None)
    assert beda.ok is False                       # чужой мост — по-прежнему беда
    assert "5539" in beda.what

    свой = check_twins(config, lines=rows, mine=9999, holder=5539)
    assert свой.ok is True                        # а свой же — нет
    assert "нет" in свой.what


def test_a_real_twin_is_still_found_next_to_the_running_bridge(config):
    """Работающий мост прикрывает только себя, а не любого соседа."""
    from bridge.doctor import check_twins

    rows = [f"5539 /home/u/most/.venv/bin/python -m bridge --name {config.name}",
            f"7100 /home/u/most/.venv/bin/python -m bridge --name {config.name}"]
    check = check_twins(config, lines=rows, mine=9999, holder=5539)
    assert check.ok is False
    assert "7100" in check.what and "5539" not in check.what
