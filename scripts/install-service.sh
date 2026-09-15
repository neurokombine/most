#!/usr/bin/env bash
# Автозапуск моста: единственное место во всей установке, где нужен sudo.
#
#   sudo bash scripts/install-service.sh            # экземпляр «default»
#   sudo bash scripts/install-service.sh anna       # экземпляр «anna»
#
# Что делает: подставляет в шаблон юнита живого пользователя и его домашнюю
# папку, кладёт юнит в /etc/systemd/system/, включает автозапуск и запускает
# мост. Идемпотентно: второй запуск просто перечитывает юнит и перезапускает.
#
# Запускать из папки моста, из-под sudo. Пользователь берётся из SUDO_USER —
# то есть мост будет работать от того, кто его ставил, а не от root: под root
# нейросеть работать отказывается, и это её защита, а не сбой.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

NAME="${1:-default}"
case "$NAME" in
  *[!A-Za-z0-9_-]*)
    echo "[автозапуск] имя экземпляра «${NAME}» не годится: только латиница, цифры, дефис"
    exit 1 ;;
esac

if [ "$(id -u)" != "0" ]; then
  echo "[автозапуск] Эту часть надо запустить через sudo — она кладёт файл службы в системную папку:"
  echo "                 sudo bash scripts/install-service.sh $NAME"
  exit 1
fi

RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
  echo "[автозапуск] Не понял, от чьего имени должен работать мост."
  echo "             Запустите так:  sudo bash scripts/install-service.sh $NAME"
  echo "             из-под обычного пользователя, а не из-под главного."
  exit 1
fi
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
if [ -z "$RUN_HOME" ] || [ ! -d "$RUN_HOME" ]; then
  echo "[автозапуск] У пользователя $RUN_USER не нашлось домашней папки — ставить некуда."
  exit 1
fi

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[автозапуск] Сначала установка, потом автозапуск: в $ROOT нет отдельного окружения."
  echo "             Запустите без sudo:  bash scripts/setup.sh $NAME"
  exit 1
fi
if [ ! -f "$RUN_HOME/.most/$NAME/config.yaml" ]; then
  echo "[автозапуск] Нет настроек $RUN_HOME/.most/$NAME/config.yaml — мост не поднимется."
  echo "             Запустите без sudo:  bash scripts/setup.sh $NAME"
  exit 1
fi

UNIT="/etc/systemd/system/most@.service"
sed -e "s|REPLACE_USER|$RUN_USER|g" -e "s|REPLACE_HOME|$RUN_HOME|g" \
    "$ROOT/systemd/most@.service" > "$UNIT"
chmod 644 "$UNIT"
echo "[автозапуск] положил службу: $UNIT (работает от $RUN_USER, папка $RUN_HOME/most)"

if [ "$ROOT" != "$RUN_HOME/most" ]; then
  echo "[автозапуск] ⚠️  мост лежит в $ROOT, а служба ищет его в $RUN_HOME/most."
  echo "             Перенесите папку моста в $RUN_HOME/most и запустите меня снова."
  exit 1
fi

systemctl daemon-reload
systemctl enable --now "most@$NAME"
sleep 2
systemctl --no-pager --full status "most@$NAME" || true

echo
echo "Готово. Дальше:"
echo "  проверить:   systemctl status most@$NAME"
echo "  посмотреть:  journalctl -u most@$NAME -n 50"
echo "  остановить:  sudo systemctl disable --now most@$NAME"
