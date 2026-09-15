"""Голос: расшифровка на самой машине и ответ вслух.

Два движка, оба необязательные и оба локальные — ключей и облачных
распознавалок в мосте нет ни в каком виде (правило блока: на сервер ученика
не едет ни один ключ).

  • **Слух** — `faster-whisper`, модель `small`, `int8`, язык русский. Замер
    на четырёх ядрах: тридцать секунд речи считаются 20,6 с, пик памяти
    785 МБ (`base` вдвое быстрее и вдвое легче, но глуше). Модель скачивается
    один раз при установке — в первом же голосовом сообщении её качать нельзя:
    человек будет сидеть перед молчащим ботом несколько минут.
  • **Речь** — `piper`: русский голос `ru_RU-irina-medium` (63 МБ) или
    `ru_RU-dmitri-medium`, файл `.onnx` рядом с `.onnx.json`.

Оба спрятаны за интерфейсами `Transcriber` и `Speaker`, и это не архитектурное
украшение: без них тесты пришлось бы гонять с моделями на диске. Библиотеки
нет — мост работает текстом и честно об этом говорит, а не падает.
"""
from __future__ import annotations

import gc
import importlib.util
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3))

DEFAULT_MODEL = "small"          # small слышит заметно лучше base, а ждать терпимо
DEFAULT_COMPUTE = "int8"         # на процессоре без видеокарты — единственный разумный
DEFAULT_LANGUAGE = "ru"
DEFAULT_PIPER_VOICE = "ru_RU-irina-medium"

MAX_SECONDS = 180                # дольше трёх минут не расшифровываем: минуты ожидания
MAX_SPEAK_CHARS = 1500           # больше вслух не читаем, остальное уходит текстом
HEARD_CHARS = 200                # сколько расшифровки показываем в «услышала: …»

WHISPER_REPO = "models--Systran--faster-whisper-{model}"
SPEECH_TIMEOUT = 180
FFMPEG_TIMEOUT = 120


class VoiceNotSetUp(RuntimeError):
    """Голос не настроен: библиотеки нет или модель не скачана."""


class VoiceTooLong(RuntimeError):
    """Запись длиннее того, что мост берётся слушать."""

    def __init__(self, message: str, seconds: float = 0.0, limit: int = MAX_SECONDS):
        super().__init__(message)
        self.seconds = seconds
        self.limit = limit


# --- общие мелочи -----------------------------------------------------------

def stamp() -> str:
    """Метка времени в имени файла — московская, как всё в мосте."""
    return datetime.now(MOSCOW).strftime("%Y-%m-%d-%H%M%S")


def fit_for_speech(text: str, limit: int = MAX_SPEAK_CHARS) -> tuple[str, bool]:
    """Что из ответа читаем вслух. Второе значение — правда ли пришлось обрезать.

    Режем по границе слова: оборванное на середине слово в наушниках звучит
    поломкой, а не сокращением.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:—-\n"), True


def shorten(text: str, limit: int = HEARD_CHARS) -> str:
    """Первые слова расшифровки для «услышала: …»."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def library_present(name: str = "faster_whisper") -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def model_ready(download_root, model: str = DEFAULT_MODEL) -> bool:
    """Скачана ли модель распознавания. Доктор спрашивает это до первого голосового."""
    named = Path(str(model)).expanduser()
    if named.is_dir():
        return (named / "model.bin").exists()

    root = Path(str(download_root or "")).expanduser()
    if not root.is_dir():
        return False
    for folder in (root / WHISPER_REPO.format(model=model), root / str(model)):
        if folder.is_dir() and any(folder.rglob("model.bin")):
            return True
    return False


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def to_ogg(path, runner=None):
    """Переводит wav в ogg/opus — то, что Telegram показывает голосовым кружком.

    Нет `ffmpeg` — возвращаем None, и тогда запись уходит обычным аудио-файлом.
    Это не беда, а другой вид сообщения: слышно одинаково.
    """
    path = Path(path)
    out = path.with_suffix(".ogg")
    runner = runner or _ffmpeg_to_ogg
    try:
        if runner(path, out) and out.exists() and out.stat().st_size:
            return out
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _ffmpeg_to_ogg(src: Path, dst: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    done = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-c:a", "libopus", "-b:a", "32k",
         "-ar", "48000", "-ac", "1", str(dst)],
        capture_output=True, timeout=FFMPEG_TIMEOUT)
    return done.returncode == 0


# --- слух -------------------------------------------------------------------

class Transcriber:
    """Интерфейс распознавания. Сам по себе — «голос не настроен»."""

    name = "нет"

    def available(self) -> bool:
        return False

    def transcribe(self, path, max_seconds: int | None = MAX_SECONDS) -> str:
        raise VoiceNotSetUp("распознавание не настроено")

    def release(self) -> bool:
        """Отпустить модель. Отпускать нечего — значит, ничего и не делаем."""
        return False

    def release_if_idle(self, after_sec: float, now: float | None = None) -> bool:
        return False


class WhisperTranscriber(Transcriber):
    """faster-whisper на процессоре.

    Модель поднимается при первом голосовом и живёт в памяти, пока ею
    пользуются: поднимать её на каждую запись — это лишние секунды перед
    каждым ответом. Но и держать вечно нельзя: после первого же голосового
    мост тяжелеет с 37 МБ до 550 (замер на сервере 15.09), а на машине из
    программы это половина свободной памяти. Поэтому после долгого простоя
    модель отпускается — обратно она поднимается около двух секунд.
    """

    name = "faster-whisper"

    def __init__(self, model: str = DEFAULT_MODEL, compute_type: str = DEFAULT_COMPUTE,
                 language: str = DEFAULT_LANGUAGE, download_root=None,
                 loader=None, max_seconds: int = MAX_SECONDS):
        self.model = model
        self.compute_type = compute_type
        self.language = language
        self.download_root = Path(download_root).expanduser() if download_root else None
        self.max_seconds = max_seconds
        self._loader = loader
        self._model = None
        self._broken = False
        self._used_at = None        # когда модель в последний раз работала

    # --- готовность ---------------------------------------------------------

    def model_on_disk(self) -> bool:
        return model_ready(self.download_root, self.model)

    def available(self) -> bool:
        if self._model is not None:
            return True
        if self._broken:
            return False
        if self._loader is not None:
            try:
                self._load()
            except Exception:                              # noqa: BLE001
                return False
            return True
        # Настоящую модель ради ответа «умею ли» не поднимаем: это секунды и
        # сотни мегабайт. Смотрим на библиотеку и на файл модели.
        return library_present() and self.model_on_disk()

    def release(self) -> bool:
        """Отпустить модель слуха. Возвращает, было ли что отпускать.

        Библиотеки (`libctranslate2`) остаются в процессе — их из памяти уже
        не выгнать. Уходят веса модели, а это основная её часть.
        """
        if self._model is None:
            return False
        self._model = None
        self._used_at = None
        gc.collect()
        return True

    def release_if_idle(self, after_sec: float, now: float | None = None) -> bool:
        """Отпустить, если ею давно не пользовались. `after_sec` 0 — не отпускать."""
        if self._model is None or not after_sec:
            return False
        now = time.monotonic() if now is None else now
        if self._used_at is None or (now - self._used_at) < after_sec:
            return False
        return self.release()

    def _load(self):
        if self._model is not None:
            self._used_at = time.monotonic()
            return self._model
        if self._broken:
            raise VoiceNotSetUp("распознавание не настроено")
        try:
            self._model = (self._loader or self._default_loader)()
        except Exception as exc:                           # noqa: BLE001
            self._broken = True
            raise VoiceNotSetUp(f"распознавание не поднялось: {exc}") from exc
        self._used_at = time.monotonic()
        return self._model

    def _default_loader(self):
        from faster_whisper import WhisperModel          # ставится отдельно

        kwargs = {"device": "cpu", "compute_type": self.compute_type}
        if self.download_root:
            kwargs["download_root"] = str(self.download_root)
        return WhisperModel(self.model, **kwargs)

    # --- работа -------------------------------------------------------------

    def transcribe(self, path, max_seconds: int | None = None) -> str:
        limit = self.max_seconds if max_seconds is None else max_seconds
        model = self._load()
        # faster-whisper отдаёт сегменты лениво, а длину записи — сразу. Значит,
        # слишком длинную запись можно отвергнуть до того, как потрачены минуты.
        segments, info = model.transcribe(str(path), language=self.language,
                                          beam_size=1, vad_filter=True)
        seconds = float(getattr(info, "duration", 0.0) or 0.0)
        if limit and seconds > limit:
            raise VoiceTooLong("запись длиннее предела", seconds=seconds, limit=limit)

        said = [str(getattr(piece, "text", "")).strip() for piece in segments]
        # Отметку ставим ПОСЛЕ работы: считаем простой от конца дела, а не от начала.
        self._used_at = time.monotonic()
        return " ".join(part for part in said if part).strip()


class FakeTranscriber(Transcriber):
    """Для тестов и отладки: говорит заранее заданное, в сеть не ходит."""

    name = "подставной слух"

    def __init__(self, text: str = "", available: bool = True, seconds: float = 0.0):
        self.text = text
        self._available = available
        self.seconds = seconds
        self.calls: list[str] = []

    def available(self) -> bool:
        return self._available

    def transcribe(self, path, max_seconds: int | None = MAX_SECONDS) -> str:
        if not self._available:
            raise VoiceNotSetUp("распознавание не настроено")
        self.calls.append(str(path))
        if max_seconds and self.seconds > max_seconds:
            raise VoiceTooLong("запись длиннее предела", seconds=self.seconds,
                               limit=max_seconds)
        return self.text


# --- речь -------------------------------------------------------------------

class Speaker:
    """Интерфейс голоса наружу. Сам по себе — «говорить нечем»."""

    name = "нет"

    def available(self) -> bool:
        return False

    def say(self, text: str, out_dir) -> Path:
        raise VoiceNotSetUp("голос наружу не настроен")


class PiperSpeaker(Speaker):
    """piper: русский голос из файла .onnx, всё считается на самой машине."""

    name = "piper"

    def __init__(self, voice_name: str = DEFAULT_PIPER_VOICE, voices_dir=None,
                 model_path=None, runner=None, limit: int = MAX_SPEAK_CHARS):
        self.voice_name = voice_name
        self.voices_dir = Path(voices_dir).expanduser() if voices_dir else None
        self._model_path = Path(model_path).expanduser() if model_path else None
        self._runner = runner
        self.limit = limit

    @property
    def model_path(self) -> Path:
        if self._model_path is not None:
            return self._model_path
        folder = self.voices_dir or Path.home() / ".most" / "voices"
        return folder / f"{self.voice_name}.onnx"

    def model_ready(self) -> bool:
        return self.model_path.exists()

    def engine_ready(self) -> bool:
        return self._runner is not None or bool(shutil.which("piper")) \
            or library_present("piper")

    def available(self) -> bool:
        return self.model_ready() and self.engine_ready()

    def say(self, text: str, out_dir) -> Path:
        if not self.available():
            raise VoiceNotSetUp("голос наружу не настроен")
        speech, _ = fit_for_speech(text, self.limit)
        if not speech:
            raise VoiceNotSetUp("читать вслух нечего")

        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"otvet-{stamp()}.wav"
        (self._runner or self._default_runner)(speech, self.model_path, out)
        if not out.exists() or not out.stat().st_size:
            raise VoiceNotSetUp("голос ничего не записал")
        return out

    @staticmethod
    def _default_runner(text: str, model_path: Path, out_path: Path) -> None:
        binary = shutil.which("piper")
        command = ([binary] if binary else [sys.executable, "-m", "piper"]) + \
            ["-m", str(model_path), "-f", str(out_path)]
        subprocess.run(command, input=text.encode("utf-8"), capture_output=True,
                       timeout=SPEECH_TIMEOUT, check=True)


class FakeSpeaker(Speaker):
    """Для тестов: пишет короткий файл и помнит, что просили прочитать."""

    name = "подставной голос"

    def __init__(self, available: bool = True):
        self._available = available
        self.said: list[str] = []

    def available(self) -> bool:
        return self._available

    def say(self, text: str, out_dir) -> Path:
        if not self._available:
            raise VoiceNotSetUp("голос наружу не настроен")
        speech, _ = fit_for_speech(text)
        self.said.append(speech)
        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"otvet-{stamp()}-{len(self.said)}.wav"
        out.write_bytes(b"RIFF" + speech.encode("utf-8")[:64])
        return out


# --- сборка по настройкам ---------------------------------------------------

def build(config) -> tuple[Transcriber, Speaker]:
    """Слух и голос по настройкам экземпляра. Выключено — пустые заглушки."""
    settings = getattr(config, "voice", None)
    if settings is None or not getattr(settings, "enabled", True):
        return Transcriber(), Speaker()

    ears = WhisperTranscriber(model=settings.model, compute_type=settings.compute_type,
                              language=settings.language,
                              download_root=settings.model_dir,
                              max_seconds=settings.max_seconds)
    mouth = PiperSpeaker(voice_name=settings.piper_voice,
                         voices_dir=settings.voices_dir,
                         limit=settings.max_chars)
    return ears, mouth
