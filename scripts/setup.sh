#!/usr/bin/env bash
# Разовая установка моста. Идемпотентно: можно запускать повторно.
# Что делает: заводит .venv, ставит две зависимости, создаёт папку экземпляра
# ~/.most/<имя>/ с заготовкой config.yaml (права 600) и папкой проектов.
#
# Запуск:  bash scripts/setup.sh            # экземпляр «default»
#          bash scripts/setup.sh anna       # экземпляр «anna»
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${1:-default}"
HOME_DIR="${MOST_HOME:-$HOME/.most/$NAME}"

# 1. базовый python: нужен 3.10–3.13 (на 3.14 колёс ещё нет)
pick_python() {
  for c in python3.12 python3.11 python3.13 python3.10 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    ver=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
    if [ "$ver" -ge 310 ] && [ "$ver" -lt 314 ]; then echo "$c"; return; fi
  done
}

if [ ! -x .venv/bin/python ]; then
  BASE=$(pick_python || true)
  if [ -z "${BASE:-}" ]; then
    echo "[установка] Не нашёл Python нужной версии. Поставьте Python 3.12 и запустите снова."
    exit 1
  fi
  echo "[установка] беру $BASE ($($BASE --version 2>&1))"
  "$BASE" -m venv .venv
fi

echo "[установка] зависимости (их всего две)…"
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

# 2. папка экземпляра
mkdir -p "$HOME_DIR/jobs"
mkdir -p "$HOME/projects"

if [ ! -f "$HOME_DIR/config.yaml" ]; then
  cat > "$HOME_DIR/config.yaml" <<YAML
# Настройки моста «${NAME}».
# Токен — только здесь; в командной строке он не появляется никогда.
# Любой из двух разделов можно удалить целиком: работает и один мессенджер.

telegram:
  token: ""          # токен от отца ботов (@BotFather)
  allowlist: []      # ваши Telegram id — пока список пуст, мост не отвечает никому

max:
  token: ""          # токен от @MasterBot
  allowlist: []      # ваши Max user_id

projects_dir: "$HOME/projects"

executor:
  kind: claude
  model: sonnet        # какой моделью работать: sonnet дешевле, opus умнее
  parallel: 1          # сколько задач вести одновременно; на 4 ГБ памяти — одна
  timeout_sec: 900     # бюджет времени на одну работу, 900 с = 15 минут
  extra_args: []       # доводы для claude; см. README, раздел «Настройки»
YAML
  echo "[установка] завёл настройки: $HOME_DIR/config.yaml"
else
  echo "[установка] настройки уже есть: $HOME_DIR/config.yaml — не трогаю"
fi

chmod 700 "$HOME_DIR"
chmod 600 "$HOME_DIR/config.yaml"

echo
echo "Готово. Дальше:"
echo "  1. впишите токен и свой id в $HOME_DIR/config.yaml"
echo "  2. проверьте:  .venv/bin/python scripts/selftest.py --name $NAME"
echo "  3. запустите:  .venv/bin/python -m bridge --name $NAME"
