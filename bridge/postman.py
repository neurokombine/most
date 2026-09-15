"""Почтальон: файлы в папку проекта и обратно.

Здесь нет ни сети, ни мессенджеров — только диск. Приёмники умеют скачать и
отправить, а куда положить, как назвать и как найти по нечёткому имени —
решается здесь, одинаково для Telegram и для Max.

Три решения, которые стоит держать в голове:

1. **Папка называется «входящие», по-русски.** Это единственное место, где
   кириллица в пути стоит нарочно: имя видит ученик в окне с папками сервера,
   и «inbox» ему ничего не скажет. Путей латиницей здесь не требуется — код
   складывает имя сам, руками его никто не набирает.
2. **Присланное не затирает присланное.** Телефон шлёт десять снимков подряд
   под именем `image.jpg`; второй файл получает «(2)», а не съедает первый.
3. **Поиск нестрогий.** Человек пишет «пришли отчёт за сентябрь», а файл
   называется `Отчёт-Сентябрь.xlsx`. Сравниваем без регистра, без «ё», по
   словам в любом порядке, в том числе внутри вложенных папок.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .changes import SKIP_DIRS

INBOX = "входящие"          # имя папки, которое видит человек
MOSCOW = timezone(timedelta(hours=3))
FIND_LIMIT = 10
RECENT_LIMIT = 10

# Чем достраиваем имя, когда мессенджер прислал файл без него (так приходят
# снимки с телефона) или без расширения.
EXTENSIONS = {"photo": ".jpg", "image": ".jpg", "video": ".mp4",
              "audio": ".m4a", "voice": ".ogg", "file": ".bin", "document": ".bin"}

BAD_CHARS = re.compile(r"[\\/\x00-\x1f]+")
SPLIT = re.compile(r"[^0-9a-zа-я]+")


# --- имена ------------------------------------------------------------------

def safe_name(name: str, kind: str = "file", when: datetime | None = None) -> str:
    """Имя файла, каким оно ляжет на диск: без путей и без сюрпризов.

    Кириллицу и пробелы оставляем как есть — это имя человека, он будет искать
    по нему глазами. Убираем только то, чем можно выйти из папки.
    """
    name = BAD_CHARS.sub("", str(name or "").strip()).strip(". ")
    if name in ("", ".", ".."):
        stamp = (when or datetime.now(MOSCOW)).strftime("%Y-%m-%d-%H%M%S")
        return f"{kind}-{stamp}{EXTENSIONS.get(kind, '.bin')}"
    if not Path(name).suffix:
        name += EXTENSIONS.get(kind, "")
    return name[:120]


def free_path(folder: Path, name: str) -> Path:
    """Свободное имя в папке: «смета.pdf», «смета (2).pdf», «смета (3).pdf»."""
    path = folder / name
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for number in range(2, 1000):
        candidate = folder / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem} ({datetime.now(MOSCOW):%H%M%S}){suffix}"


# --- входящие ---------------------------------------------------------------

def inbox_dir(workdir: Path | str) -> Path:
    folder = Path(workdir) / INBOX
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_incoming(workdir: Path | str, name: str, data: bytes, kind: str = "file") -> Path:
    """Кладёт присланное в «входящие» и отдаёт путь, который можно назвать вслух."""
    folder = inbox_dir(workdir)
    path = free_path(folder, safe_name(name, kind=kind))
    path.write_bytes(data)
    return path


# --- размеры ----------------------------------------------------------------

def human_size(size) -> str:
    """Размер словами: человеку нужны «3,5 МБ», а не 3 670 016."""
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "размер неизвестен"
    if size < 1024:
        return f"{size} байт"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} КБ"
    megabytes = size / (1024 * 1024)
    if megabytes < 10:
        return f"{megabytes:.1f} МБ".replace(".", ",").replace(",0 ", " ")
    return f"{round(megabytes)} МБ"


# --- поиск ------------------------------------------------------------------

def _plain(text: str) -> str:
    """Одинаково для файла и для вопроса: без регистра, без «ё», без знаков."""
    return SPLIT.sub(" ", str(text or "").lower().replace("ё", "е")).strip()


def _words(text: str) -> list[str]:
    return [w for w in _plain(text).split() if w]


def walk_files(workdir: Path | str):
    """Файлы папки проекта без служебного мусора: .git, venv, node_modules."""
    import os

    root = Path(workdir)
    if not root.is_dir():
        return
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            yield Path(base) / name


def find_files(workdir: Path | str, query: str, limit: int = FIND_LIMIT) -> list[Path]:
    """Файлы, похожие на названное человеком. Свежие — первыми."""
    words = _words(query)
    if not words:
        return []

    found: list[tuple[float, Path]] = []
    for path in walk_files(workdir):
        haystack = _plain(path.name)
        if not all(word in haystack for word in words):
            # Слова могли разъехаться по папке и имени: «отчёты/сентябрь.xlsx».
            try:
                haystack = _plain(path.relative_to(Path(workdir)).as_posix())
            except ValueError:
                continue
            if not all(word in haystack for word in words):
                continue
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue

    found.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in found[:limit]]


def recent_files(workdir: Path | str, limit: int = RECENT_LIMIT) -> list[Path]:
    """Что вообще лежит в папке — показываем, когда названного не нашлось."""
    found: list[tuple[float, Path]] = []
    for path in walk_files(workdir):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in found[:limit]]


def relative(path: Path, workdir: Path | str) -> str:
    """Как назвать файл человеку: путь от папки проекта, а не от корня диска."""
    try:
        return Path(path).relative_to(Path(workdir)).as_posix()
    except ValueError:
        return Path(path).name


def size_of(path: Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0
