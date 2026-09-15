"""Настройки одного экземпляра моста: ~/.most/<имя>/config.yaml.

Экземпляр — это один человек с одним ботом (или двумя: Telegram и Max).
Своя папка, своя база, свои токены. Токены живут только в этом файле
и никогда не попадают ни в командную строку, ни в логи.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import texts, voice

DEFAULT_ROOT = Path.home() / ".most"
DEFAULT_TIMEOUT_SEC = 900      # бюджет времени на одну работу — 15 минут
DEFAULT_MODEL = "sonnet"       # headless сам берёт Opus 1M; для моста это дорого
DEFAULT_PARALLEL = 1           # на 4 ГБ у ученика двух claude сразу не бывает
DEFAULT_TIMEZONE = "Europe/Moscow"   # всё время моста — московское, пока не сказано иное
DEFAULT_SUMMARY_AT = "08:00"   # ежедневная сводка — в восемь утра по этой зоне
DEFAULT_TICK_SEC = 30          # как часто будильник смотрит на часы


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
class VoiceConfig:
    """Голос: слушать ли записи и читать ли ответы вслух.

    Модели лежат рядом с экземплярами, а не внутри каждого: полгигабайта на
    человека — расточительство, а голос у всех один и тот же.
    """

    enabled: bool = True                       # слушать голосовые, если есть чем
    reply: bool = False                        # читать вслух сводки; по просьбе — всегда
    model: str = voice.DEFAULT_MODEL
    compute_type: str = voice.DEFAULT_COMPUTE
    language: str = voice.DEFAULT_LANGUAGE
    piper_voice: str = voice.DEFAULT_PIPER_VOICE
    max_seconds: int = voice.MAX_SECONDS
    max_chars: int = voice.MAX_SPEAK_CHARS
    model_dir: Path = field(default_factory=lambda: DEFAULT_ROOT / "models" / "faster-whisper")
    voices_dir: Path = field(default_factory=lambda: DEFAULT_ROOT / "voices")


@dataclass
class ScheduleConfig:
    """Расписание: ежедневная сводка и частота взгляда на часы.

    Время здесь и во всех задачах — по `Config.timezone`, то есть московское,
    пока человек не сказал иначе. Зона машины не спрашивается никогда: сервер
    у ученика стоит где угодно, а живёт он в Москве.
    """

    summary: bool = True                   # слать ли ежедневную сводку
    summary_at: str = DEFAULT_SUMMARY_AT   # во сколько (ЧЧ:ММ по зоне моста)
    tick_sec: int = DEFAULT_TICK_SEC


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
    executor_model: str = DEFAULT_MODEL
    executor_extra_args: list[str] = field(default_factory=list)
    parallel: int = DEFAULT_PARALLEL
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    timezone: str = DEFAULT_TIMEZONE
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

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


# Папка самого моста. Она лежит рядом с рабочими папками человека — и, если
# его проекты лежат прямо в домашней папке (а после переезда системы это самый
# обычный случай), мост предложил бы сам себя как папку для работы. Своей
# папкой мост не работает никогда.
BRIDGE_ROOT = Path(__file__).resolve().parent.parent


def work_folders(root) -> list[str]:
    """Рабочие папки человека: всё видимое внутри, кроме папки самого моста."""
    root = Path(root)
    if not root.exists():
        return []
    found = []
    for item in sorted(root.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        try:
            if item.resolve() == BRIDGE_ROOT:
                continue
        except OSError:
            pass
        found.append(item.name)
    return found


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


def _voice(raw, home: Path) -> VoiceConfig:
    """Раздел voice. Нет раздела — значит, всё по умолчанию: слушаем, вслух молчим."""
    shared = home.parent if home.parent != home else DEFAULT_ROOT
    settings = VoiceConfig(model_dir=shared / "models" / "faster-whisper",
                           voices_dir=shared / "voices")
    if not isinstance(raw, dict):
        return settings

    settings.enabled = bool(raw.get("enabled", True))
    settings.reply = bool(raw.get("reply", False))
    settings.model = str(raw.get("model") or settings.model)
    settings.compute_type = str(raw.get("compute_type") or settings.compute_type)
    settings.language = str(raw.get("language") or settings.language)
    settings.piper_voice = str(raw.get("piper_voice") or settings.piper_voice)
    settings.max_seconds = _int(raw.get("max_seconds"), voice.MAX_SECONDS, least=5)
    settings.max_chars = _int(raw.get("max_chars"), voice.MAX_SPEAK_CHARS, least=50)
    for key in ("model_dir", "voices_dir"):
        value = raw.get(key)
        if value:
            setattr(settings, key, Path(os.path.expanduser(str(value))))
    return settings


def _schedule(raw) -> ScheduleConfig:
    """Раздел schedule. Нет раздела — сводка в восемь утра, и это нормально."""
    settings = ScheduleConfig()
    if not isinstance(raw, dict):
        return settings
    settings.summary = bool(raw.get("summary", True))
    settings.summary_at = _hhmm(raw.get("summary_at"), DEFAULT_SUMMARY_AT)
    settings.tick_sec = _int(raw.get("tick_sec"), DEFAULT_TICK_SEC, least=1)
    return settings


def _hhmm(value, default: str) -> str:
    """«08:00» — да, «утром» — нет. Непонятное время не роняет мост, а отступает."""
    raw = str(value or "").strip()
    parts = raw.replace(".", ":").split(":")
    if len(parts) == 2:
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            return default
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"{hour:02d}:{minute:02d}"
    return default


def _int(value, default: int, least: int = 1) -> int:
    try:
        return max(least, int(value))
    except (TypeError, ValueError):
        return default


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

    # Исполнитель: строкой (старый вид) или разделом с моделью и доводами.
    raw_executor = raw.get("executor")
    if isinstance(raw_executor, dict):
        kind = str(raw_executor.get("kind") or "claude")
        model = str(raw_executor.get("model") or DEFAULT_MODEL)
        extra = [str(a) for a in (raw_executor.get("extra_args") or [])]
        parallel = _int(raw_executor.get("parallel"), DEFAULT_PARALLEL)
        timeout = _int(raw_executor.get("timeout_sec") or raw.get("timeout_sec"),
                       DEFAULT_TIMEOUT_SEC)
    else:
        kind = str(raw_executor or "claude")
        model, extra = DEFAULT_MODEL, []
        parallel = _int(raw.get("parallel"), DEFAULT_PARALLEL)
        timeout = _int(raw.get("timeout_sec"), DEFAULT_TIMEOUT_SEC)

    return Config(
        name=name,
        home=home,
        config_path=path,
        projects_dir=projects_dir,
        executor=kind,
        telegram=telegram,
        max=maxx,
        timeout_sec=timeout,
        executor_model=model,
        executor_extra_args=extra,
        parallel=parallel,
        voice=_voice(raw.get("voice"), home),
        timezone=str(raw.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
        schedule=_schedule(raw.get("schedule")),
    )


# --- белый список правится на месте, а пояснения в файле остаются ------------
# Список своих живёт в config.yaml, и он — источник правды: при запуске мост
# переливает его в базу. Значит, «пусти меня» должно доходить до файла, иначе
# человек снова станет чужим после перезагрузки. Правим файл строками, а не
# перезаписью через yaml: в заготовке живут пояснения, ради которых её и писали,
# а `yaml.safe_dump` вычистил бы их все.

ALLOWLIST_RE = re.compile(r"^(?P<indent>\s*)allowlist\s*:\s*(?P<rest>.*)$")
ITEM_RE = re.compile(r"^\s*-\s*(?P<id>\d+)")


def _channel_block(lines: list[str], channel: str) -> tuple[int, int] | None:
    """Где в файле лежит раздел мессенджера: от строки «telegram:» до следующего раздела."""
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(channel)}\s*:\s*(#.*)?$", line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.strip() and not line[:1].isspace() and not line.lstrip().startswith("#"):
            end = j
            break
    return start, end


def _inline_ids(rest: str) -> tuple[list[int], str] | None:
    """Разбирает «[111, 222]  # свои» на числа и хвост-пояснение."""
    if not rest.startswith("["):
        return None
    close = rest.find("]")
    if close < 0:
        return None
    ids = [int(n) for n in re.findall(r"\d+", rest[1:close])]
    return ids, rest[close + 1:]


def _verify(path: Path, channel: str, want: list[int], body: str) -> bool:
    """Правку принимаем, только если файл после неё читается и список тот самый."""
    try:
        raw = yaml.safe_load(body) or {}
        got = [int(x) for x in ((raw.get(channel) or {}).get("allowlist") or [])]
    except (yaml.YAMLError, TypeError, ValueError, AttributeError):
        return False
    return got == want


def _save(path: Path, body: str) -> None:
    """Пишем на место, не открывая файл с токеном чужим глазам."""
    mode = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        pass
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode if mode is not None else 0o600)


def _write_allowlist(path: Path, channel: str, user_id: int, add: bool) -> str:
    """Общая часть «пусти» и «не пускай». Ответ — словом, чтобы было что сказать человеку."""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return "failed"
    lines = body.splitlines()
    block = _channel_block(lines, channel)
    if block is None:
        return "no_channel"
    start, end = block

    where = None
    for i in range(start + 1, end):
        found = ALLOWLIST_RE.match(lines[i])
        if found:
            where = (i, found)
            break

    user_id = int(user_id)
    if where is None:
        if not add:
            return "already"
        lines.insert(start + 1, f"  allowlist: [{user_id}]")
        ids = [user_id]
    else:
        i, found = where
        indent, rest = found.group("indent"), found.group("rest").strip()
        inline = _inline_ids(rest)
        if inline is not None:
            ids, tail = inline
            if (user_id in ids) == add:
                return "already"
            ids = ids + [user_id] if add else [n for n in ids if n != user_id]
            lines[i] = f"{indent}allowlist: [{', '.join(str(n) for n in ids)}]{tail}"
        else:
            # Список строками: «allowlist:» и ниже «  - 111».
            item_lines = []
            for j in range(i + 1, end):
                if ITEM_RE.match(lines[j]):
                    item_lines.append(j)
                elif lines[j].strip() and not lines[j].lstrip().startswith("#"):
                    break
            ids = [int(ITEM_RE.match(lines[j]).group("id")) for j in item_lines]
            if (user_id in ids) == add:
                return "already"
            if add:
                step = lines[item_lines[-1]] if item_lines else f"{indent}  - 0"
                mark = step[:len(step) - len(step.lstrip())]
                lines.insert((item_lines[-1] if item_lines else i) + 1, f"{mark}- {user_id}")
                ids = ids + [user_id]
            else:
                for j in reversed(item_lines):
                    if int(ITEM_RE.match(lines[j]).group("id")) == user_id:
                        lines.pop(j)
                ids = [n for n in ids if n != user_id]
                if not ids:
                    lines[i] = f"{indent}allowlist: []"

    fresh = "\n".join(lines) + ("\n" if body.endswith("\n") else "")
    if not _verify(path, channel, ids, fresh):
        return "failed"
    _save(path, fresh)
    return "added" if add else "removed"


def add_to_allowlist(path: Path | str, channel: str, user_id: int) -> str:
    """Вписывает человека в свои. Ответ: added · already · no_channel · failed."""
    return _write_allowlist(Path(path), channel, user_id, add=True)


def remove_from_allowlist(path: Path | str, channel: str, user_id: int) -> str:
    """Убирает человека из своих. Ответ: removed · already · no_channel · failed."""
    return _write_allowlist(Path(path), channel, user_id, add=False)
