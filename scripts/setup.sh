#!/usr/bin/env bash
# Разовая установка моста. Идемпотентно: можно запускать повторно.
# Что делает: заводит .venv, ставит две зависимости, создаёт папку экземпляра
# ~/.most/<имя>/ с заготовкой config.yaml (права 600) и папкой проектов.
#
# Запуск:  bash scripts/setup.sh                        # экземпляр «default»
#          bash scripts/setup.sh anna                   # экземпляр «anna»
#          bash scripts/setup.sh anna --projects ~/moi-proekty
#          bash scripts/setup.sh anna --voice           # вместе с голосом
#
# «--projects» — папка, в которой лежат рабочие папки человека. Она у него уже
# есть после переезда системы: мост ничего не переносит и ничего там не трогает,
# он только знает, где искать.
#
# С «--voice» ставятся ещё две библиотеки и заранее скачиваются модели: слух
# (~500 МБ) и голос (~63 МБ). Скачать их надо ИМЕННО сейчас, а не при первом
# голосовом сообщении: иначе человек сидит перед молчащим ботом несколько минут.
# На машине с 4 ГБ памяти голос впритык — по умолчанию он выключен.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="default"
WITH_VOICE=-1          # -1 = «решай по памяти машины», 0 = без голоса, 1 = с голосом
PROJECTS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --voice) WITH_VOICE=1 ;;
    --no-voice|--bez-golosa) WITH_VOICE=0 ;;
    --projects)
      shift
      [ $# -gt 0 ] || { echo "[установка] после --projects нужен путь к папке с проектами"; exit 1; }
      PROJECTS="$1" ;;
    --projects=*) PROJECTS="${1#--projects=}" ;;
    -*) echo "[установка] не знаю довода $1 — знаю --voice, --no-voice и --projects <папка>"; exit 1 ;;
    *) NAME="$1" ;;
  esac
  shift
done

# Голос по умолчанию — да, если машина его тянет. Решение 3 плана и приёмка
# 15.09: человек, которому мост сказал «понимаю голосовое», а потом «повторите
# текстом», считает это поломкой, и он прав. Порог живёт в scripts/pamyat.py.
if [ "$WITH_VOICE" = "-1" ]; then
  if python3 "$(dirname "$0")/pamyat.py" --tiho 2>/dev/null; then
    WITH_VOICE=1
    echo "[установка] голос ставлю: памяти на машине хватает ($(python3 "$(dirname "$0")/pamyat.py" 2>/dev/null | sed 's/память машины: //'))"
  else
    WITH_VOICE=0
    echo "[установка] голос НЕ ставлю: на этой машине памяти маловато."
    echo "            Это не поломка: голосовые мост честно попросит повторить текстом,"
    echo "            а всё остальное работает как обычно. Скажите об этом человеку."
    echo "            Захочет всё равно — bash scripts/setup.sh $NAME --voice"
  fi
fi

case "$NAME" in
  *[!A-Za-z0-9_-]*)
    echo "[установка] имя экземпляра «${NAME}» не годится: только латиница, цифры, дефис"
    echo "            имя попадает в имя службы most@<имя> и в путь ~/.most/<имя>"
    exit 1 ;;
esac

HOME_DIR="${MOST_HOME:-$HOME/.most/${NAME}}"
SHARED_DIR="$(dirname "$HOME_DIR")"           # модели общие для всех экземпляров
MODELS_DIR="$SHARED_DIR/models/faster-whisper"
VOICES_DIR="$SHARED_DIR/voices"
PIPER_VOICE="${MOST_PIPER_VOICE:-ru_RU-irina-medium}"
WHISPER_MODEL="${MOST_WHISPER_MODEL:-small}"

# Папка проектов: сказали — берём сказанное, не сказали — ~/projects.
PROJECTS="${PROJECTS:-$HOME/projects}"
case "$PROJECTS" in
  "~") PROJECTS="$HOME" ;;
  "~/"*) PROJECTS="$HOME/${PROJECTS#\~/}" ;;
esac

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
    echo "[установка] Не нашёл Python нужной версии (нужен от 3.10 до 3.13)."
    echo "            На Ubuntu:  sudo apt install -y python3 python3-venv"
    exit 1
  fi
  echo "[установка] беру $BASE ($($BASE --version 2>&1))"
  if ! "$BASE" -m venv .venv; then
    # Самая частая беда голой Ubuntu: сам python есть, а части для окружения нет.
    echo "[установка] Отдельное окружение не создалось: у этого Python нет части venv."
    echo "            На Ubuntu это лечится одной строкой:"
    echo "                sudo apt install -y python3-venv"
    echo "            После этого запустите установку снова — она продолжит с этого места."
    rm -rf .venv
    exit 1
  fi
fi

echo "[установка] зависимости (их всего две)…"
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

# 2. папка экземпляра
mkdir -p "$HOME_DIR/jobs"
if [ ! -d "$PROJECTS" ]; then
  mkdir -p "$PROJECTS"
  echo "[установка] папки проектов не было — завёл пустую: $PROJECTS"
  echo "            если ваши рабочие папки лежат в другом месте, поправьте"
  echo "            строку projects_dir в $HOME_DIR/config.yaml"
else
  echo "[установка] папка проектов: $PROJECTS"
fi

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

# Папка, внутри которой лежат ваши рабочие папки. Мост в них работает,
# но ничего туда не переносит.
projects_dir: "$PROJECTS"

timezone: "Europe/Moscow"   # время расписания и всех отметок

voice:
  enabled: true           # слушать голосовые, если распознавание поставлено
  reply: false            # читать вслух сводки; «ответь голосом» работает всегда
  model: small            # small слышит лучше, base — вдвое быстрее и легче
  piper_voice: $PIPER_VOICE
  max_seconds: 180        # запись длиннее трёх минут мост не разбирает
  max_chars: 1500         # больше этого вслух не читаем, остальное уходит текстом

executor:
  kind: claude
  model: sonnet        # какой моделью работать: sonnet дешевле, opus умнее
  parallel: 1          # сколько задач вести одновременно; на 4 ГБ памяти — одна
  timeout_sec: 900     # бюджет времени на одну работу, 900 с = 15 минут
  # Не тянуть в работу личные расширения и шпаргалки: запуск дешевле и
  # предсказуемее, CLAUDE.md самой рабочей папки при этом читается как обычно.
  extra_args: ["--setting-sources", "project"]
YAML
  echo "[установка] завёл настройки: $HOME_DIR/config.yaml"
else
  echo "[установка] настройки уже есть: $HOME_DIR/config.yaml — не трогаю"
  if ! grep -q "projects_dir:.*$PROJECTS" "$HOME_DIR/config.yaml" 2>/dev/null; then
    echo "            папка проектов в них своя; если нужна «${PROJECTS}» — поправьте строку projects_dir"
  fi
fi

chmod 700 "$HOME_DIR"
chmod 600 "$HOME_DIR/config.yaml"

# 3. голос: библиотеки и модели — заранее, а не в первом сообщении
if [ "$WITH_VOICE" = "1" ]; then
  echo "[голос] ставлю распознавание и чтение вслух (это займёт несколько минут)…"
  .venv/bin/python -m pip install -q -r requirements-voice.txt

  mkdir -p "$MODELS_DIR" "$VOICES_DIR"
  echo "[голос] скачиваю модель распознавания «${WHISPER_MODEL}» в ${MODELS_DIR}…"
  .venv/bin/python - "$WHISPER_MODEL" "$MODELS_DIR" <<'PYCODE'
import sys
from faster_whisper import WhisperModel

model, root = sys.argv[1], sys.argv[2]
WhisperModel(model, device="cpu", compute_type="int8", download_root=root)
print(f"[голос] модель «{model}» на месте")
PYCODE

  BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU"
  SPEAKER="${PIPER_VOICE#ru_RU-}"; SPEAKER="${SPEAKER%-*}"
  QUALITY="${PIPER_VOICE##*-}"
  for suffix in ".onnx" ".onnx.json"; do
    target="$VOICES_DIR/${PIPER_VOICE}$suffix"
    if [ -s "$target" ]; then
      echo "[голос] ${PIPER_VOICE}$suffix уже есть — не качаю"
      continue
    fi
    echo "[голос] скачиваю ${PIPER_VOICE}${suffix}…"
    curl -fsSL "$BASE_URL/$SPEAKER/$QUALITY/${PIPER_VOICE}$suffix" -o "$target" || {
      echo "[голос] голос $PIPER_VOICE скачать не вышло — мост будет слушать, но отвечать текстом"
      rm -f "$target"
    }
  done

  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[голос] ffmpeg не найден: ответы уйдут аудио-файлом, а не голосовым кружком."
    echo "        Поставить:  sudo apt install -y ffmpeg"
  fi
fi

echo
echo "Готово. Дальше:"
echo "  1. впишите токены в $HOME_DIR/config.yaml (список своих оставьте пустым)"
echo "  2. проверьте:  .venv/bin/python -m bridge --name $NAME doctor"
echo "  3. автозапуск: sudo bash scripts/install-service.sh $NAME"
if [ "$WITH_VOICE" != "1" ]; then
  echo
  echo "Голос не ставился. Если он всё-таки нужен:"
  echo "  bash scripts/setup.sh $NAME --voice"
  echo "  На машине с 4 ГБ памяти он впритык: слух занимает 620–970 МБ."
fi
