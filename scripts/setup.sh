#!/usr/bin/env bash
# Разовая установка моста. Идемпотентно: можно запускать повторно.
# Что делает: заводит .venv, ставит две зависимости, создаёт папку экземпляра
# ~/.most/<имя>/ с заготовкой config.yaml (права 600) и папкой проектов.
#
# Запуск:  bash scripts/setup.sh                  # экземпляр «default»
#          bash scripts/setup.sh anna             # экземпляр «anna»
#          bash scripts/setup.sh --voice          # вместе с голосом
#
# С «--voice» ставятся ещё две библиотеки и заранее скачиваются модели: слух
# (~500 МБ) и голос (~63 МБ). Скачать их надо ИМЕННО сейчас, а не при первом
# голосовом сообщении: иначе человек сидит перед молчащим ботом несколько минут.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="default"
WITH_VOICE=0
for arg in "$@"; do
  case "$arg" in
    --voice) WITH_VOICE=1 ;;
    -*) echo "[установка] не знаю довода $arg — знаю только --voice"; exit 1 ;;
    *) NAME="$arg" ;;
  esac
done

HOME_DIR="${MOST_HOME:-$HOME/.most/${NAME}}"
SHARED_DIR="$(dirname "$HOME_DIR")"           # модели общие для всех экземпляров
MODELS_DIR="$SHARED_DIR/models/faster-whisper"
VOICES_DIR="$SHARED_DIR/voices"
PIPER_VOICE="${MOST_PIPER_VOICE:-ru_RU-irina-medium}"
WHISPER_MODEL="${MOST_WHISPER_MODEL:-small}"

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
  extra_args: []       # доводы для claude; см. README, раздел «Настройки»
YAML
  echo "[установка] завёл настройки: $HOME_DIR/config.yaml"
else
  echo "[установка] настройки уже есть: $HOME_DIR/config.yaml — не трогаю"
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
    echo "[голос] скачиваю ${PIPER_VOICE}$suffix…"
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
echo "  1. впишите токен и свой id в $HOME_DIR/config.yaml"
echo "  2. проверьте:  .venv/bin/python scripts/selftest.py --name $NAME"
echo "  3. запустите:  .venv/bin/python -m bridge --name $NAME"
if [ "$WITH_VOICE" != "1" ]; then
  echo
  echo "Голос (расшифровка голосовых и ответ вслух) ставится отдельно:"
  echo "  bash scripts/setup.sh $NAME --voice"
fi
