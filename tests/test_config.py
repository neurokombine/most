"""Конфиг экземпляра: чтение, необязательность каналов, права на файл."""
import os
import stat

import pytest

from bridge.config import load_config, ConfigError


def write(path, text, mode=0o600):
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_reads_both_channels(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111, 222]
max:
  token: "max-abc"
  allowlist: [333]
projects_dir: "{projects_dir}"
executor: claude
""")
    cfg = load_config(home=home, name="test")
    assert cfg.telegram.token == "abc:123"
    assert cfg.telegram.allowlist == [111, 222]
    assert cfg.max.allowlist == [333]
    assert cfg.projects_dir == projects_dir
    assert cfg.db_path == home / "most.db"
    assert cfg.jobs_dir == home / "jobs"


def test_max_section_may_be_absent(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
projects_dir: "{projects_dir}"
""")
    cfg = load_config(home=home, name="test")
    assert cfg.max is None
    assert cfg.telegram.allowlist == []
    assert cfg.enabled_channels() == ["telegram"]


def test_telegram_section_may_be_absent(home, projects_dir):
    write(home / "config.yaml", f"""
max:
  token: "max-abc"
projects_dir: "{projects_dir}"
""")
    cfg = load_config(home=home, name="test")
    assert cfg.telegram is None
    assert cfg.enabled_channels() == ["max"]


def test_no_channels_at_all_is_an_error(home, projects_dir):
    write(home / "config.yaml", f'projects_dir: "{projects_dir}"\n')
    with pytest.raises(ConfigError):
        load_config(home=home, name="test")


def test_missing_file_is_a_human_error(home):
    with pytest.raises(ConfigError) as exc:
        load_config(home=home, name="test")
    assert "config.yaml" in str(exc.value)


def test_loose_permissions_are_reported(home, projects_dir):
    path = write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
projects_dir: "{projects_dir}"
""", mode=0o644)
    cfg = load_config(home=home, name="test")
    assert cfg.permissions_are_loose() is True
    os.chmod(path, 0o600)
    assert cfg.permissions_are_loose() is False


def test_tokens_never_appear_in_repr(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "super-secret-token"
projects_dir: "{projects_dir}"
""")
    cfg = load_config(home=home, name="test")
    assert "super-secret-token" not in repr(cfg)
    assert "super-secret-token" not in str(cfg.telegram)


def test_projects_dir_defaults_next_to_home(home):
    write(home / "config.yaml", 'telegram:\n  token: "abc"\n')
    cfg = load_config(home=home, name="test")
    assert cfg.projects_dir.name == "projects"
