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


# --- этап 2: исполнитель настраивается, а не зашит --------------------------

def test_executor_defaults_are_sane_when_nothing_is_said(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
""")
    cfg = load_config(home=home, name="test")
    assert cfg.executor == "claude"
    assert cfg.executor_model == "sonnet"       # не Opus: headless берёт самую дорогую
    assert cfg.executor_extra_args == []
    assert cfg.parallel == 1                    # на 4 ГБ ученика двух claude не бывает
    assert cfg.timeout_sec == 900               # бюджет времени на работу: 15 минут


def test_executor_section_sets_model_parallel_and_extra_args(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
executor:
  kind: claude
  model: opus
  parallel: 2
  timeout_sec: 300
  extra_args: ["--setting-sources", "project"]
""")
    cfg = load_config(home=home, name="test")
    assert cfg.executor == "claude"
    assert cfg.executor_model == "opus"
    assert cfg.parallel == 2
    assert cfg.timeout_sec == 300
    assert cfg.executor_extra_args == ["--setting-sources", "project"]


def test_old_style_executor_line_still_works(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
executor: claude
timeout_sec: 120
""")
    cfg = load_config(home=home, name="test")
    assert cfg.executor == "claude"
    assert cfg.timeout_sec == 120


def test_nonsense_parallel_does_not_break_the_bridge(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
executor:
  parallel: "сколько получится"
""")
    cfg = load_config(home=home, name="test")
    assert cfg.parallel == 1


# --- голос ------------------------------------------------------------------

def test_voice_settings_have_sane_defaults(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
""")
    cfg = load_config(home=home, name="test")
    assert cfg.voice.enabled is True
    assert cfg.voice.model == "small"
    assert cfg.voice.reply is False              # голосом наружу — только по просьбе
    assert cfg.voice.max_seconds == 180
    assert cfg.voice.max_chars == 1500
    assert cfg.voice.piper_voice.startswith("ru_RU-")
    # модели общие для всех экземпляров: 500 МБ на каждого — расточительство
    assert cfg.voice.model_dir == home.parent / "models" / "faster-whisper"
    assert cfg.voice.voices_dir == home.parent / "voices"


def test_voice_section_is_read(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
voice:
  enabled: true
  model: base
  reply: true
  piper_voice: ru_RU-dmitri-medium
  max_seconds: 120
  max_chars: 900
  model_dir: "{projects_dir}/models"
  voices_dir: "{projects_dir}/voices"
""")
    cfg = load_config(home=home, name="test")
    assert cfg.voice.model == "base"
    assert cfg.voice.reply is True
    assert cfg.voice.piper_voice == "ru_RU-dmitri-medium"
    assert cfg.voice.max_seconds == 120
    assert cfg.voice.max_chars == 900
    assert cfg.voice.model_dir == projects_dir / "models"
    assert cfg.voice.voices_dir == projects_dir / "voices"


def test_voice_can_be_switched_off(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
voice:
  enabled: false
""")
    cfg = load_config(home=home, name="test")
    assert cfg.voice.enabled is False


def test_nonsense_voice_limits_do_not_break_the_bridge(home, projects_dir):
    write(home / "config.yaml", f"""
telegram:
  token: "abc:123"
  allowlist: [111]
projects_dir: "{projects_dir}"
voice:
  max_seconds: "три минуты"
  max_chars: null
""")
    cfg = load_config(home=home, name="test")
    assert cfg.voice.max_seconds == 180
    assert cfg.voice.max_chars == 1500
