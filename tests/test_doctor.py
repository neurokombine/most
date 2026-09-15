"""Доктор: заготовка этапа 6 — токен и сеть. Сети в тестах нет."""
from bridge.doctor import Check, check_claude, check_config, check_network, checkup
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
