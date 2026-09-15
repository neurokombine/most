"""Сборка моста из настроек: что настроено, то и слушаем."""
import os

from bridge.__main__ import build_bridge, main
from bridge.config import load_config


def write_config(home, body, mode=0o600):
    path = home / "config.yaml"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_only_configured_channels_are_listened_to(home, projects_dir):
    write_config(home, f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
""")
    cfg = load_config(home=home, name="test")
    bridge = build_bridge(cfg)
    assert set(bridge.receivers) == {"telegram"}
    bridge.store.close()


def test_both_channels_are_listened_to(home, projects_dir):
    write_config(home, f"""
telegram:
  token: "abc:123"
  allowlist: [111]
max:
  token: "max-abc"
  allowlist: [222]
projects_dir: "{projects_dir}"
""")
    bridge = build_bridge(load_config(home=home, name="test"))
    assert set(bridge.receivers) == {"telegram", "max"}
    bridge.store.close()


def test_allowlist_from_the_file_lands_in_the_base(home, projects_dir):
    write_config(home, f"""
telegram:
  token: "abc:123"
  allowlist: [111, 222]
projects_dir: "{projects_dir}"
""")
    bridge = build_bridge(load_config(home=home, name="test"))
    assert bridge.store.is_allowed("telegram", 222) is True
    assert bridge.store.is_allowed("telegram", 333) is False
    bridge.store.close()


def test_missing_config_exits_quietly_with_a_human_text(home, capsys):
    code = main(["--home", str(home), "--name", "test"])
    assert code == 0                       # чинится руками, респавн не поможет
    out = capsys.readouterr().out
    assert "config.yaml" in out
    assert "Traceback" not in out


def test_executor_settings_reach_the_real_executor(home, projects_dir):
    write_config(home, f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
executor:
  model: opus
  parallel: 2
  timeout_sec: 60
  extra_args: ["--setting-sources", "project"]
""")
    bridge = build_bridge(load_config(home=home, name="test"))
    assert bridge.executor.model == "opus"
    assert bridge.executor.timeout == 60
    assert bridge.executor.extra_args == ["--setting-sources", "project"]
    assert bridge.pool.max_parallel == 2
    bridge.store.close()


# --- этап 6: второй мост того же экземпляра ---------------------------------

def test_the_second_bridge_of_the_same_name_exits_quietly(home, projects_dir, capsys):
    """Причина третья: двое слушают одного бота. Второй не поднимается вовсе."""
    from bridge import lock

    write_config(home, f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
""")
    held = lock.InstanceLock(home / "most.lock")
    assert held.acquire() is True
    try:
        code = main(["--home", str(home), "--name", "test", "--once"])
    finally:
        held.release()

    assert code == 0                       # чинится руками, респавн не поможет
    out = capsys.readouterr().out
    assert "уже запущен" in out
    assert "Traceback" not in out


def test_the_lock_is_let_go_after_the_run(home, projects_dir):
    from bridge import lock

    write_config(home, f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
""")
    main(["--home", str(home), "--name", "test", "--once"])
    second = lock.InstanceLock(home / "most.lock")
    assert second.acquire() is True        # мост вышел — замок отпущен
    second.release()
