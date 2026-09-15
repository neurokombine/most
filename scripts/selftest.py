#!/usr/bin/env python3
"""Самопроверка моста: всё ли на месте, чтобы он заработал.

Проверяет настройки и права на файл с токеном, папку проектов, нейросеть
(`claude --version`) и дорогу до мессенджеров, которые у вас настроены.
Ничего не запускает и ничего не меняет — только смотрит и рассказывает.

Запуск:
    .venv/bin/python scripts/selftest.py                 # экземпляр «default»
    .venv/bin/python scripts/selftest.py --name anna     # другой экземпляр
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.config import ConfigError, load_config      # noqa: E402
from bridge.doctor import checkup                       # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Самопроверка моста")
    parser.add_argument("--name", default="default", help="имя экземпляра")
    parser.add_argument("--home", default=None, help="папка экземпляра целиком")
    args = parser.parse_args()

    print("Смотрю, всё ли готово к работе.\n")
    try:
        config = load_config(home=Path(args.home) if args.home else None, name=args.name)
    except ConfigError as exc:
        print(f"Не в порядке: {exc}")
        print("\nПока настроек нет, проверять нечего. Заведите config.yaml и позовите меня снова.")
        return 1

    checks = checkup(config)
    for check in checks:
        print(check.line())

    beda = [c for c in checks if not c.ok]
    print()
    if not beda:
        print("Всё в порядке — мост можно запускать:")
        print(f"    .venv/bin/python -m bridge --name {config.name}")
        return 0
    print(f"Не в порядке: {len(beda)} из {len(checks)}. Почините перечисленное и позовите меня снова.")
    print("Если непонятно, что делать, — покажите этот вывод целиком своей нейросети.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
