"""Доктор: самодиагностика одной фразой.

Полностью он разворачивается на этапе 6 (юнит, конфликт вебхука, место на
диске, память, последние отказы). Здесь — то, без чего нельзя запускаться:
настройки и права на файл с токеном, наличие нейросети, живая ли сеть до
мессенджера. Каждая проверка отвечает по-человечески: что в порядке, что нет
и что с этим делать.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

from . import texts, voice
from .executor import clean_env, resolve_claude_bin
from .receivers.base import mask
from .receivers.max import BASE as MAX_BASE
from .receivers.max import CA_BUNDLE

PROBE = {
    "telegram": "https://api.telegram.org/bot{token}/getMe",
    "max": MAX_BASE + "/me",
}


@dataclass
class Check:
    ok: bool
    what: str
    hint: str | None = None

    def line(self) -> str:
        head = (texts.SELFTEST_OK if self.ok else texts.SELFTEST_BAD).format(what=self.what)
        if self.ok or not self.hint:
            return head
        return head + "\n" + texts.SELFTEST_HINT.format(hint=self.hint)


def check_config(config) -> Check:
    path = Path(config.config_path)
    if not path.exists():
        return Check(False, f"настройки не найдены: {path}",
                     "создайте config.yaml с токеном бота и списком своих id")
    if config.permissions_are_loose():
        return Check(False, f"файл с токеном открыт другим пользователям машины: {path}",
                     f"chmod 600 {path}")
    channels = ", ".join(config.enabled_channels())
    return Check(True, f"настройки читаются, каналы: {channels}, права закрыты")


def check_projects(config) -> Check:
    root = Path(config.projects_dir)
    if not root.exists():
        return Check(False, f"папки проектов нет: {root}",
                     f"создайте её: mkdir -p {root}")
    folders = [p.name for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    if not folders:
        return Check(False, f"в папке проектов пусто: {root}",
                     "заведите внутри папку под дело — с ней мост и будет работать")
    return Check(True, f"папок проектов: {len(folders)} (первая — «{folders[0]}»)")


def check_claude(claude_bin: str | None = None) -> Check:
    claude_bin = claude_bin or resolve_claude_bin()
    if not Path(claude_bin).exists() and not shutil.which(str(claude_bin)):
        return Check(False, f"нейросеть claude не найдена: {claude_bin}",
                     "поставьте Claude Code и войдите в аккаунт по подписке")
    try:
        done = subprocess.run([str(claude_bin), "--version"], capture_output=True,
                              text=True, timeout=30, env=clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(False, f"claude не запускается: {exc}",
                     "проверьте установку Claude Code")
    if done.returncode != 0:
        return Check(False, "claude отвечает ошибкой на --version",
                     (done.stderr or done.stdout or "").strip()[:200] or "проверьте установку")
    return Check(True, f"нейросеть на месте: {(done.stdout or '').strip()}")


def check_voice(config, library=None) -> Check:
    """Голос: есть ли чем слушать и скачана ли модель.

    Библиотеки нет — это не беда, а выбор: мост работает текстом и на голосовое
    честно просит повторить словами. А вот «библиотека есть, модели нет» — это
    полудело: первое же голосовое встанет на несколько минут скачивания, и
    человек будет сидеть перед молчащим ботом. Такое называем неисправностью.
    """
    settings = getattr(config, "voice", None)
    if settings is None or not settings.enabled:
        return Check(True, "голос выключен в настройках — мост работает текстом")

    present = voice.library_present() if library is None else bool(library)
    if not present:
        return Check(True, "голос не поставлен: голосовые мост попросит повторить текстом",
                     "включить: bash scripts/setup.sh --voice")
    if not voice.model_ready(settings.model_dir, settings.model):
        return Check(False, f"голос поставлен, а модель «{settings.model}» не скачана: "
                            f"{settings.model_dir}",
                     "скачайте её заранее: bash scripts/setup.sh --voice "
                     "(в первом голосовом это минуты ожидания)")

    speaker = voice.PiperSpeaker(voice_name=settings.piper_voice,
                                 voices_dir=settings.voices_dir)
    out = "и читает вслух" if speaker.available() else "наружу молчит (piper не поставлен)"
    return Check(True, f"голос слышит моделью «{settings.model}», {out}")


def check_network(channel: str, session=None, token: str = "") -> Check:
    """Живая ли дорога до мессенджера. Токен в текст не попадает никогда."""
    if not (token or "").strip():
        return Check(False, f"токен {channel} не вписан в настройки",
                     "возьмите токен у @BotFather (Telegram) или у @MasterBot (Max) "
                     "и впишите его в config.yaml")
    session = session or requests.Session()
    url = PROBE[channel].format(token=token)
    kwargs = {"timeout": 20}
    if channel == "max":
        kwargs["headers"] = {"Authorization": token}
        kwargs["verify"] = CA_BUNDLE
    try:
        resp = session.get(url, **kwargs)
    except requests.exceptions.RequestException as exc:
        return Check(False, f"нет сети до {channel}: {mask(exc, [token])}",
                     "проверьте интернет на сервере и настройки DNS")
    if resp.status_code in (401, 403):
        return Check(False, f"{channel} не признал токен",
                     texts.TELEGRAM_TOKEN_REJECTED if channel == "telegram"
                     else texts.MAX_TOKEN_REJECTED)
    if resp.status_code == 409:
        return Check(False, f"{channel}: слушателя перехватили", texts.TELEGRAM_CONFLICT)
    if not (200 <= resp.status_code < 300):
        return Check(False, f"{channel} отвечает кодом {resp.status_code}",
                     "похоже, беда на их стороне — подождите и проверьте снова")
    return Check(True, f"сеть до {channel} есть, бот отзывается")


def checkup(config, session=None, claude_bin: str | None = None) -> list[Check]:
    """Полный обход. Сессию и путь к claude можно подменить — так его зовут тесты."""
    checks = [check_config(config), check_projects(config), check_claude(claude_bin),
              check_voice(config)]
    for channel in config.enabled_channels():
        token = getattr(config.channel(channel), "token", "")
        checks.append(check_network(channel, session=session, token=token))
    return checks
