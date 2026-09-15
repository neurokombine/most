"""Замок экземпляра и соседи по машине.

Два моста одного экземпляра на одной машине — это не «вдвое надёжнее», а
молчащий бот: мессенджер отдаёт обновления одному слушателю, и второй отбирает
их у первого. Поэтому мост берёт замок на свою папку и, если он занят, честно
выходит кодом 0 с человеческим текстом — респавн юнита тут не поможет.

Здесь же — то, чем доктор смотрит на соседей: жив ли процесс с таким номером,
чем он занят и нет ли рядом второго моста с тем же именем.
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
from pathlib import Path

CLAUDE_MARK = "claude"
PS_TIMEOUT = 10
# Команды моста (`status`, `allow` …) — это вопросы к базе, а не слушатели бота:
# в списке процессов они выглядят почти так же, но вторым мостом не являются.
BRIDGE_COMMANDS = ("knock", "allow", "deny", "who", "status", "doctor", "say")


class InstanceLock:
    """Замок на папку экземпляра: `~/.most/<имя>/most.lock`.

    Замок держится, пока жив процесс: умер мост — ядро отпускает его само,
    и файл с чужим номером никого не блокирует (это важнее аккуратности:
    мост, не поднявшийся из-за забытого файла, чинится вручную ночью).
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        """True — замок наш. False — экземпляр с этим именем уже запущен."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return True

    def holder_pid(self) -> int | None:
        """Чей замок сейчас. None — значит, свободен и мост не запущен."""
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            return None
        return pid if alive(pid) else None

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def alive(pid) -> bool:
    """Жив ли процесс с таким номером."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                 # чужой, но живой
    except OSError:
        return False
    return True


def command_of(pid) -> str:
    """Чем занят процесс. Пусто — значит, его уже нет или спросить не вышло."""
    if not alive(pid):
        return ""
    try:
        done = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                              capture_output=True, text=True, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return (done.stdout or "").strip()


def looks_like_claude(pid, command: str | None = None) -> bool:
    """Тот ли это процесс, который мы собираемся гасить.

    Номера процессов переиспользуются: работа в базе значится живой со вчера,
    а под её номером сегодня работает чужая программа. Гасим только то, в чём
    узнаём нейросеть.
    """
    text = command_of(pid) if command is None else command
    return CLAUDE_MARK in (text or "").lower()


def kill_tree(pid) -> bool:
    """Гасим всю группу процессов: claude поднимает детей, одинокий kill их бросит."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except OSError:
                return sig is signal.SIGKILL
        if not alive(pid):
            return True
    return not alive(pid)


def all_processes() -> list[str]:
    try:
        done = subprocess.run(["ps", "-eo", "pid=,command="], capture_output=True,
                              text=True, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in (done.stdout or "").splitlines() if line.strip()]


def other_bridges(name: str, mine: int | None = None, lines=None) -> list[tuple[int, str]]:
    """Другие мосты того же экземпляра на этой машине.

    Это вторая причина молчания из ворот 2: «этого бота уже слушает кто-то ещё».
    Смотрим по списку процессов, а не только по замку: старый мост мог быть
    запущен из другой папки, с другим файлом замка — а бота он всё равно отобрал.
    """
    mine = os.getpid() if mine is None else int(mine)
    rows = all_processes() if lines is None else list(lines)
    out: list[tuple[int, str]] = []
    for line in rows:
        text = line.strip()
        if "-m bridge" not in text and "bridge/__main__" not in text:
            continue
        if "grep" in text:
            continue
        head, _, rest = text.partition(" ")
        try:
            pid = int(head)
        except (TypeError, ValueError):
            continue
        if pid == mine:
            continue
        parts = rest.split()
        if not parts:
            continue
        # Запускает мост python, а не оболочка: строка запуска видна и у того
        # `sh -c`, из которого мост позвали, — и он мостом не является.
        if not Path(parts[0]).name.startswith("python"):
            continue
        # Команда моста — не слушатель: она спрашивает базу и выходит.
        if any(part in BRIDGE_COMMANDS for part in parts[1:]):
            continue
        if f"--name {name}" not in rest and not (name == "default" and "--name" not in rest):
            continue
        out.append((pid, text))
    return out
