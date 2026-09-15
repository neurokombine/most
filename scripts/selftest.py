#!/usr/bin/env python3
"""Самопроверка моста: всё ли на месте, чтобы он заработал.

Проверяет настройки и права на файл с токеном, папку проектов, нейросеть,
голос, дорогу до мессенджеров, запущен ли мост и нет ли второго, часы против
московских, память и место на диске. Ничего не запускает и ничего не меняет —
только смотрит и рассказывает. С «--live» ещё и спрашивает нейросеть живьём:
это тратит подписку, поэтому само собой не делается никогда.

То же самое делает команда моста: `python -m bridge --name default doctor`.

Запуск:
    .venv/bin/python scripts/selftest.py                 # экземпляр «default»
    .venv/bin/python scripts/selftest.py --name anna     # другой экземпляр
    .venv/bin/python scripts/selftest.py --live          # ещё и спросить нейросеть
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.config import ConfigError, load_config              # noqa: E402
from bridge.doctor import checkup, table, verdict               # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Самопроверка моста")
    parser.add_argument("--name", default="default", help="имя экземпляра")
    parser.add_argument("--home", default=None, help="папка экземпляра целиком")
    parser.add_argument("--live", action="store_true",
                        help="спросить нейросеть живьём (тратит подписку)")
    args = parser.parse_args()

    print("Смотрю, всё ли готово к работе.\n")
    try:
        config = load_config(home=Path(args.home) if args.home else None, name=args.name)
    except ConfigError as exc:
        print(f"Не в порядке: {exc}")
        print("\nПока настроек нет, проверять нечего. Заведите config.yaml и позовите меня снова.")
        return 1

    checks = checkup(config, live=args.live)
    for line in table(checks):
        print(line)

    beda = [c for c in checks if not c.ok]
    print()
    print(verdict(checks, config))
    if not beda:
        print(f"    .venv/bin/python -m bridge --name {config.name}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
