"""Запуск моста: python -m bridge --name <имя экземпляра>.

Экземпляр — это человек со своим ботом: свои настройки ~/.most/<имя>/config.yaml,
своя база, своя папка проектов. На одной машине их может быть сколько угодно,
юнит шаблонный: most@<имя>.service.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import texts
from .config import ConfigError, load_config
from .daemon import Bridge
from .executor import ClaudeExecutor
from .receivers.max import MaxReceiver
from .receivers.telegram import TelegramReceiver
from .store import Store


def build_bridge(config) -> Bridge:
    """Собирает мост по настройкам: слушаем только то, что настроено."""
    store = Store(config.db_path).init()

    receivers = {}
    if config.telegram is not None:
        store.sync_allowlist("telegram", config.telegram.allowlist)
        receivers["telegram"] = TelegramReceiver(token=config.telegram.token, store=store)
    if config.max is not None:
        store.sync_allowlist("max", config.max.allowlist)
        receivers["max"] = MaxReceiver(token=config.max.token, store=store)

    executor = ClaudeExecutor(jobs_dir=config.jobs_dir, timeout=config.timeout_sec,
                              secrets=config.secrets())
    return Bridge(config=config, store=store, executor=executor, receivers=receivers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Мост между мессенджером и вашей системой")
    parser.add_argument("--name", default="default", help="имя экземпляра (папка ~/.most/<имя>)")
    parser.add_argument("--home", default=None, help="папка экземпляра целиком (для отладки)")
    parser.add_argument("--once", action="store_true", help="один заход опроса и выход")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else None
    try:
        config = load_config(home=home, name=args.name)
    except ConfigError as exc:
        # Ненастроенный мост не поднимают перезапуском: код 0, чтобы юнит
        # не ушёл в цикл респавна, и текст, по которому понятно, что делать.
        print(f"мост: {exc}", flush=True)
        return 0

    if config.permissions_are_loose():
        print("мост: " + texts.CONFIG_LOOSE_PERMISSIONS.format(path=config.config_path),
              flush=True)

    bridge = build_bridge(config)
    channels = ", ".join(config.enabled_channels()) or "нет"
    print(f"мост «{config.name}»: слушаю {channels}; папка проектов {config.projects_dir}",
          flush=True)
    try:
        return bridge.run(max_ticks=1 if args.once else None)
    except KeyboardInterrupt:
        print("мост: остановлен с клавиатуры", flush=True)
        return 0
    finally:
        bridge.store.close()


if __name__ == "__main__":
    sys.exit(main())
