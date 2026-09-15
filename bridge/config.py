"""Настройки одного экземпляра моста: ~/.most/<имя>/config.yaml.

Экземпляр — это один человек с одним ботом (или двумя: Telegram и Max).
Своя папка, своя база, свои токены. Токены живут только в этом файле
и никогда не попадают ни в командную строку, ни в логи.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import texts

DEFAULT_ROOT = Path.home() / ".most"
DEFAULT_TIMEOUT_SEC = 900


class ConfigError(RuntimeError):
    """Настройки не прочитались. Текст — уже человеческий, его можно показывать."""


@dataclass
class ChannelConfig:
    """Один мессенджер: токен и список своих id."""

    token: str
    allowlist: list[int] = field(default_factory=list)

    def __repr__(self) -> str:          # токен не показываем никогда
        return f"ChannelConfig(token=***, allowlist={self.allowlist})"

    __str__ = __repr__


@dataclass
class Config:
    name: str
    home: Path
    config_path: Path
    projects_dir: Path
    executor: str = "claude"
    telegram: ChannelConfig | None = None
    max: ChannelConfig | None = None
    timeout_sec: int = DEFAULT_TIMEOUT_SEC

    # --- производные пути ---------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.home / "most.db"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "jobs"

    def channel(self, name: str) -> ChannelConfig | None:
        return self.telegram if name == "telegram" else self.max

    def enabled_channels(self) -> list[str]:
        return [n for n in ("telegram", "max") if self.channel(n) is not None]

    def secrets(self) -> list[str]:
        """Всё, что нельзя показывать в логе."""
        return [c.token for c in (self.telegram, self.max) if c and c.token]

    def permissions_are_loose(self) -> bool:
        """Файл с токеном виден кому-то, кроме владельца."""
        try:
            mode = self.config_path.stat().st_mode
        except OSError:
            return False
        return bool(mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH))

    def __repr__(self) -> str:
        return (f"Config(name={self.name!r}, home={self.home}, "
                f"projects_dir={self.projects_dir}, channels={self.enabled_channels()})")

    __str__ = __repr__


def instance_home(name: str, root: Path | None = None) -> Path:
    return (root or DEFAULT_ROOT) / name


def _channel(raw: dict | None) -> ChannelConfig | None:
    if not isinstance(raw, dict):
        return None
    token = str(raw.get("token") or "").strip()
    allowlist = []
    for item in raw.get("allowlist") or []:
        try:
            allowlist.append(int(item))
        except (TypeError, ValueError):
            continue
    return ChannelConfig(token=token, allowlist=allowlist)


def load_config(home: Path | None = None, name: str = "default",
                root: Path | None = None) -> Config:
    """Читает config.yaml экземпляра. Любая беда — ConfigError с текстом для человека."""
    home = Path(home) if home else instance_home(name, root)
    path = home / "config.yaml"
    if not path.exists():
        raise ConfigError(texts.CONFIG_MISSING.format(path=path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(texts.CONFIG_BROKEN.format(path=path, problem=exc)) from exc
    if not isinstance(raw, dict):
        raise ConfigError(texts.CONFIG_BROKEN.format(path=path, problem="ожидался список настроек"))

    telegram = _channel(raw.get("telegram"))
    maxx = _channel(raw.get("max"))
    if telegram is None and maxx is None:
        raise ConfigError(texts.CONFIG_NO_CHANNELS.format(path=path))

    projects = raw.get("projects_dir")
    projects_dir = Path(os.path.expanduser(str(projects))) if projects else (home / "projects")

    timeout = raw.get("timeout_sec") or DEFAULT_TIMEOUT_SEC
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC

    return Config(
        name=name,
        home=home,
        config_path=path,
        projects_dir=projects_dir,
        executor=str(raw.get("executor") or "claude"),
        telegram=telegram,
        max=maxx,
        timeout_sec=timeout,
    )
