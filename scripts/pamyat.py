#!/usr/bin/env python3
"""Сколько на машине памяти и хватает ли её на голос.

Отдельный файл и только стандартная библиотека нарочно: это спрашивает
`setup.sh` системным питоном, до того как заведено отдельное окружение
и поставлено хоть что-нибудь.

Порог — 6 ГБ всей памяти машины, а не свободной. Замеры: расшифровка одного
голосового держит 622 МБ на сервере и до 970 МБ на Mac, нейросеть на время
работы — около 300 МБ, сам мост — 35 МБ. На машине из программы (4 ГБ) это
помещается только впритык и только по одному делу за раз, поэтому там голос
не ставим молча, а говорим человеку, чего он лишается и чем это включить.

    python3 scripts/pamyat.py            # печатает «хватает» или «мало» и объём
    python3 scripts/pamyat.py --tiho     # молча; код 0 — хватает, 1 — мало
"""
from __future__ import annotations

import sys
from pathlib import Path

GIB = 1024 ** 3
VOICE_FLOOR = 6 * GIB       # меньше шести гигабайт — голос по умолчанию не ставим


def total_memory(meminfo: str | None = None) -> int | None:
    """Вся память машины в байтах. None — измерить не вышло, и это не беда."""
    try:
        text = meminfo if meminfo is not None else Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return _macos_memory()
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1]) * 1024
            except (ValueError, IndexError):
                return None
    return None


def _macos_memory() -> int | None:
    """На Mac /proc нет. Мост там не живёт, но репетируют установку как раз на нём."""
    try:
        import subprocess
        done = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                              text=True, timeout=5)
        return int(done.stdout.strip()) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def enough_for_voice(total: int | None = -1) -> bool:
    """Хватает ли памяти на голос. Не смогли измерить — считаем, что хватает:
    молча лишать человека голоса из-за неудавшегося замера неправильно."""
    if total == -1:
        total = total_memory()
    if total is None:
        return True
    return total >= VOICE_FLOOR


def main(argv: list[str]) -> int:
    total = total_memory()
    ok = enough_for_voice(total)
    if "--tiho" not in argv:
        skolko = f"{total / GIB:.1f} ГБ" if total else "не измерить"
        print(f"память машины: {skolko} — "
              f"{'хватает на голос' if ok else 'на голос маловато'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
