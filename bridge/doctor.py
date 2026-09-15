"""Доктор: самодиагностика одной фразой.

Его зовут, когда бот молчит, — и он обязан назвать причину сам, а не показать
номер ошибки. Смотрит по кругу: настройки и права на файл с токеном, папку
проектов, нейросеть и живой ли вход по подписке, голос, дорогу до каждого
мессенджера, замок экземпляра и второй мост на машине, часы против московских,
память и место на диске.

Каждая проверка отвечает тремя вещами: в порядке или нет, что именно, и что
с этим делать. Никаких номеров кодов: человеку сорока с лишним лет «ошибка 409»
не говорит ничего, а «этого бота уже слушает кто-то ещё» — говорит всё.

Живой вопрос нейросети (`--live`) тратит подписку, поэтому он делается только
по прямой просьбе и никогда — сам собой.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from . import alarm, lock, texts, voice
from .executor import clean_env, resolve_claude_bin
from .receivers.base import mask
from .receivers.max import BASE as MAX_BASE
from .receivers.max import CA_BUNDLE

# Меньше полутора гигабайт свободной памяти — это уже разговор про голос и
# про вторую работу разом: замер разведки — 300 МБ на `claude` и 930 МБ на
# распознавание. Меньше гигабайта свободного диска — журналы работ встанут.
MEMORY_FLOOR = 1_536 * 1024 * 1024
DISK_FLOOR = 1024 * 1024 * 1024
CLOCK_DRIFT = 120           # расхождение часов больше двух минут уже двигает расписание
LIVE_TIMEOUT = 60
LIVE_PROMPT = "скажи ок"
GIB = 1024 ** 3

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


# --- машина: замок, соседи, часы, память, диск ------------------------------

def check_lock(config) -> Check:
    """Запущен ли мост. Это не поломка, а состояние: доктора зовут и до запуска."""
    pid = lock.InstanceLock(Path(config.home) / "most.lock").holder_pid()
    if pid:
        return Check(True, f"мост запущен, процесс {pid}")
    return Check(True, "мост не запущен — если бот молчит, причина может быть в этом",
                 f"запустите: systemctl --user start most@{config.name} "
                 f"(или вручную: python -m bridge --name {config.name})")


def check_twins(config, lines=None, mine: int | None = None) -> Check:
    """Второй мост того же экземпляра — вторая причина молчания из трёх.

    Мессенджер отдаёт сообщения одному слушателю. Пока их двое, они отбирают
    сообщения друг у друга, и бот молчит через раз или молчит совсем.
    """
    twins = lock.other_bridges(config.name, mine=mine, lines=lines)
    if not twins:
        return Check(True, "второго моста с этим именем на машине нет")
    who = ", ".join(str(pid) for pid, _ in twins)
    return Check(False, f"этого бота уже слушает кто-то ещё: второй мост «{config.name}» "
                        f"на этой же машине (процесс {who})",
                 "один бот — один слушатель. Остановите лишний мост: "
                 f"kill {twins[0][0]} — и бот заговорит снова")


def check_clock(config, machine: datetime | None = None) -> Check:
    """Часы машины против московских: показываем оба времени и не спорим.

    Мост считает время московским всегда, откуда бы ни был сервер. Но если
    часы самой машины ушли вперёд или назад, расписание поедет вместе с ними —
    вот это и есть беда, а разные пояса бедой не являются.
    """
    machine = machine or datetime.now().astimezone()
    moscow = alarm.now(config)
    drift = abs((machine - moscow).total_seconds())
    both = (f"часы моста: {alarm.when_text(moscow)}; "
            f"часы машины: {machine.strftime('%d.%m %H:%M')} "
            f"(пояс {machine.strftime('%z') or 'не назван'})")
    if drift > CLOCK_DRIFT:
        return Check(False, f"часы машины разошлись с настоящим временем на "
                            f"{int(drift // 60)} мин — {both}",
                     "поправьте время на сервере (обычно это timedatectl set-ntp true), "
                     "иначе расписание будет срабатывать не тогда, когда сказано")
    return Check(True, both,
                 "мост всё считает по Москве, пояс машины ни на что не влияет")


def free_memory() -> int | None:
    """Сколько памяти свободно прямо сейчас. None — измерить не вышло."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        done = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10)
        size, free = 4096, 0
        for line in (done.stdout or "").splitlines():
            if "page size of" in line:
                size = int("".join(ch for ch in line.split("page size of")[1] if ch.isdigit()))
            for mark in ("Pages free:", "Pages inactive:", "Pages speculative:"):
                if line.startswith(mark):
                    free += int(line.split(":")[1].strip().rstrip("."))
        return free * size if free else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def check_memory(free: int | None = -1) -> Check:
    """Памяти должно хватать на нейросеть и на распознавание разом."""
    free = free_memory() if free == -1 else free
    if free is None:
        return Check(True, "сколько памяти свободно, я не смогла измерить — "
                           "посмотрите сами, если мост будет обрываться")
    gib = free / GIB
    if free < MEMORY_FLOOR:
        return Check(False, f"свободной памяти мало: {gib:.1f} ГБ",
                     "голосовые на такой машине лучше выключить (voice.enabled: false) "
                     "и держать parallel: 1 — одна работа за раз. Иначе нейросеть "
                     "и распознавание не поместятся вместе и работа оборвётся")
    return Check(True, f"свободной памяти {gib:.1f} ГБ — хватит")


def check_disk(config, free: int | None = None) -> Check:
    """Место под журналы работ и под то, что присылают в чат."""
    if free is None:
        # Папки экземпляра может ещё не быть — смотрим ближайшую, которая есть:
        # диск-то у них один.
        where = Path(config.home)
        while not where.exists() and where != where.parent:
            where = where.parent
        try:
            free = shutil.disk_usage(where).free
        except OSError:
            return Check(True, "сколько места на диске, я не смогла измерить")
    gib = free / GIB
    if free < DISK_FLOOR:
        return Check(False, f"на диске почти не осталось места: {gib:.1f} ГБ",
                     "уберите старые журналы работ из ~/.most/*/jobs — "
                     "без места мост не сможет ни записать работу, ни принять файл")
    return Check(True, f"места на диске {gib:.1f} ГБ")


def check_live(config, runner=None, claude_bin: str | None = None) -> Check:
    """Живой вопрос нейросети: жив ли вход по подписке. Тратит подписку — только по просьбе."""
    claude_bin = claude_bin or resolve_claude_bin()
    runner = runner or subprocess.run
    cmd = [str(claude_bin), "-p", LIVE_PROMPT, "--model", "sonnet"]
    try:
        done = runner(cmd, capture_output=True, text=True, timeout=LIVE_TIMEOUT,
                      env=clean_env())
    except subprocess.TimeoutExpired:
        return Check(False, f"нейросеть не ответила за {LIVE_TIMEOUT} с",
                     "попробуйте ещё раз; если повторится — машина слишком занята "
                     "или интернет на сервере еле дышит")
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(False, f"нейросеть не запустилась: {exc}",
                     "проверьте установку Claude Code")
    if done.returncode != 0:
        return Check(False, "нейросеть не ответила — похоже, вход в аккаунт не действует",
                     "войдите заново: запустите claude в окне редактора на сервере "
                     "и выполните вход по подписке, как при установке")
    said = (done.stdout or "").strip().replace("\n", " ")[:80]
    return Check(True, f"вход по подписке живой, нейросеть ответила: «{said}»")


def checkup(config, session=None, claude_bin: str | None = None, live: bool = False,
            store=None) -> list[Check]:
    """Полный обход. Сессию и путь к claude можно подменить — так его зовут тесты."""
    checks = [check_config(config), check_projects(config), check_claude(claude_bin),
              check_voice(config)]
    for channel in config.enabled_channels():
        token = getattr(config.channel(channel), "token", "")
        checks.append(check_network(channel, session=session, token=token))
    checks += [check_lock(config), check_twins(config), check_clock(config),
               check_memory(), check_disk(config)]
    if live:
        # Живой вопрос стоит денег подписки — задаём его только по просьбе.
        checks.append(check_live(config, claude_bin=claude_bin))
    return checks


def table(checks: list[Check]) -> list[str]:
    """Вывод доктора таблицей: в порядке · нет · что сделать."""
    return [line for check in checks for line in check.line().split("\n")]


def verdict(checks: list[Check], config=None) -> str:
    bad = [c for c in checks if not c.ok]
    if not bad:
        return texts.SELFTEST_ALL_GOOD
    return texts.SELFTEST_TROUBLE.format(bad=len(bad), all=len(checks))
