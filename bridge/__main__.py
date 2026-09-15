"""Запуск моста: python -m bridge --name <имя экземпляра>.

Экземпляр — это человек со своим ботом: свои настройки ~/.most/<имя>/config.yaml,
своя база, своя папка проектов. На одной машине их может быть сколько угодно,
юнит шаблонный: most@<имя>.service.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import cli, lock, texts
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
                              secrets=config.secrets(), model=config.executor_model,
                              extra_args=config.executor_extra_args)
    return Bridge(config=config, store=store, executor=executor, receivers=receivers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Мост между мессенджером и вашей системой",
        epilog="без команды — запуск моста; команды: " + ", ".join(cli.COMMANDS))
    parser.add_argument("--name", default="default", help="имя экземпляра (папка ~/.most/<имя>)")
    parser.add_argument("--home", default=None, help="папка экземпляра целиком (для отладки)")
    parser.add_argument("--once", action="store_true", help="один заход опроса и выход")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="ответ команды разбором для машины")
    parser.add_argument("--live", action="store_true",
                        help="doctor: спросить нейросеть живьём (тратит подписку)")
    parser.add_argument("command", nargs="?", default=None,
                        help="что сделать: " + ", ".join(cli.COMMANDS))
    parser.add_argument("rest", nargs="*", help="доводы команды")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else None
    try:
        config = load_config(home=home, name=args.name)
    except ConfigError as exc:
        # Ненастроенный мост не поднимают перезапуском: код 0, чтобы юнит
        # не ушёл в цикл респавна, и текст, по которому понятно, что делать.
        # А вот команде отвечаем единицей: её задал человек и ждёт ответа.
        print(f"мост: {exc}", flush=True)
        return 1 if args.command else 0

    if args.command:
        # Команда — это вопрос к базе моста, а не второй мост: замок она не
        # берёт, обновления у мессенджера не спрашивает, бота не отбирает.
        return cli.run(args.command, args.rest, config=config, as_json=args.as_json,
                       live=args.live)

    if config.permissions_are_loose():
        print("мост: " + texts.CONFIG_LOOSE_PERMISSIONS.format(path=config.config_path),
              flush=True)

    # Один мост на экземпляр. Второй отобрал бы у первого обновления — и бот
    # замолчал бы для обоих. Выходим кодом 0: респавн юнита тут не поможет.
    held = lock.InstanceLock(config.home / "most.lock")
    if not held.acquire():
        print("мост: " + texts.ALREADY_RUNNING.format(
            name=config.name, pid=held.holder_pid() or "неизвестен"), flush=True)
        return 0

    bridge = build_bridge(config)
    channels = ", ".join(texts.CHANNEL_NAMES.get(c, c)
                         for c in config.enabled_channels()) or "нет"
    print(f"мост «{config.name}»: слушаю {channels}; папка проектов {config.projects_dir}; "
          f"нейросеть {config.executor_model}, работ за раз {config.parallel}, "
          f"бюджет {config.timeout_sec // 60} мин", flush=True)
    try:
        return bridge.run(max_ticks=1 if args.once else None)
    except KeyboardInterrupt:
        print("мост: остановлен с клавиатуры", flush=True)
        return 0
    finally:
        # Сначала гасим работы, потом базу: закрыть её из-под живого потока —
        # уронить процесс целиком.
        bridge.pool.stop_all()
        bridge.pool.wait_idle(timeout=30)
        bridge.store.close()
        held.release()


if __name__ == "__main__":
    sys.exit(main())
