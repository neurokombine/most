"""Что изменилось в папке проекта после последней работы.

Смотрит сам мост, без нейросети, — и в этом весь смысл. Нейросеть рассказывает,
что она сделала; папка показывает, что в ней появилось. Когда эти два ответа
расходятся — это и есть «отчёт ≠ результат», которое ученик должен увидеть
своими глазами, а не принять на веру.

Служебные папки не показываем: ученику нужны его файлы, а не `.git` и venv.
"""
from __future__ import annotations

import os
from pathlib import Path

LIMIT = 20

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next",
    ".cache", "dist", "build", ".claude",
}


def changed_files(workdir: Path | str, since: float, limit: int = LIMIT) -> list[tuple[str, float]]:
    """Файлы новее метки `since`, свежие сверху. Путь — относительно папки проекта."""
    root = Path(workdir)
    if not root.is_dir():
        return []

    found: list[tuple[str, float]] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in files:
            if name.startswith("."):
                continue
            path = Path(base) / name
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > since:
                found.append((path.relative_to(root).as_posix(), mtime))

    found.sort(key=lambda pair: pair[1], reverse=True)
    return found[:limit]


def mentioned_files(text: str, workdir: Path | str, since: float,
                    limit: int = 5) -> list[Path]:
    """Файлы, которые нейросеть назвала в ответе и которые правда изменились.

    Почему не верим словам на слово: «сохранила в отчёт.xlsx» она может сказать
    и не сохранив. Поэтому берём пересечение двух списков — что названо в ответе
    и что на самом деле стало свежее начала работы. Свежие первыми.
    """
    said = (text or "").lower()
    if not said:
        return []

    root = Path(workdir)
    out: list[Path] = []
    for name, _mtime in changed_files(root, since=since, limit=200):
        if name.lower() in said or Path(name).name.lower() in said:
            out.append(root / name)
        if len(out) >= limit:
            break
    return out
