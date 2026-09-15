"""Голос: расшифровка на самой машине и ответ вслух.

Ни один тест не ставит моделей и не ходит в сеть: и распознавание, и голос
наружу спрятаны за интерфейсом, а вместо них в тестах подставные движки.
Проверяем ровно то, что решает поведение моста: деградацию без библиотеки,
предел длины, кириллицу и обрезку для озвучки.
"""
import time

import pytest

from bridge import voice


# --- предел длины речи наружу ----------------------------------------------

def test_short_text_goes_to_speech_as_is():
    text, cut = voice.fit_for_speech("Готово: отчёт собран.")
    assert text == "Готово: отчёт собран."
    assert cut is False


def test_long_text_is_cut_to_the_limit():
    text, cut = voice.fit_for_speech("а" * (voice.MAX_SPEAK_CHARS + 500))
    assert cut is True
    assert len(text) <= voice.MAX_SPEAK_CHARS


def test_cut_happens_on_a_word_boundary():
    words = ("сентябрь " * 400).strip()
    text, cut = voice.fit_for_speech(words)
    assert cut is True
    assert not text.endswith("сен")          # слово не разорвано посередине
    assert text.endswith("сентябрь")


def test_empty_text_is_not_spoken():
    assert voice.fit_for_speech("   ") == ("", False)


# --- расшифровка: подставной движок ----------------------------------------

class FakeModel:
    """Похож на faster_whisper.WhisperModel настолько, насколько мост его трогает."""

    def __init__(self, segments=("Привет, посчитай остатки",), duration=12.0):
        self.segments = segments
        self.duration = duration
        self.calls = []

    def transcribe(self, path, **kw):
        self.calls.append({"path": str(path), **kw})
        info = type("Info", (), {"duration": self.duration, "language": "ru"})()
        seg = type("Seg", (), {})
        pieces = []
        for text in self.segments:
            s = seg()
            s.text = text
            pieces.append(s)
        return iter(pieces), info


def test_transcriber_returns_cyrillic_text(tmp_path):
    sound = tmp_path / "голос.ogg"
    sound.write_bytes(b"opus")
    model = FakeModel(segments=(" Посчитай ", "остатки по сентябрю"))
    t = voice.WhisperTranscriber(loader=lambda: model)

    assert t.transcribe(sound) == "Посчитай остатки по сентябрю"
    assert model.calls[0]["language"] == "ru"


def test_transcriber_refuses_too_long_recording(tmp_path):
    sound = tmp_path / "долгий.ogg"
    sound.write_bytes(b"opus")
    t = voice.WhisperTranscriber(loader=lambda: FakeModel(duration=200.0))

    with pytest.raises(voice.VoiceTooLong) as beda:
        t.transcribe(sound, max_seconds=180)
    assert beda.value.seconds == pytest.approx(200.0)


def test_transcriber_does_not_load_the_model_twice(tmp_path):
    sound = tmp_path / "г.ogg"
    sound.write_bytes(b"opus")
    loads = []

    def loader():
        loads.append(1)
        return FakeModel()

    t = voice.WhisperTranscriber(loader=loader)
    t.transcribe(sound)
    t.transcribe(sound)
    assert len(loads) == 1


def test_transcriber_without_the_library_is_not_available(tmp_path):
    def loader():
        raise ImportError("нет faster_whisper")

    t = voice.WhisperTranscriber(loader=loader)
    assert t.available() is False
    with pytest.raises(voice.VoiceNotSetUp):
        t.transcribe(tmp_path / "нет.ogg")


def test_model_on_disk_is_seen(tmp_path):
    root = tmp_path / "models"
    (root / "models--Systran--faster-whisper-small" / "snapshots" / "abc").mkdir(parents=True)
    (root / "models--Systran--faster-whisper-small" / "snapshots" / "abc" / "model.bin").write_bytes(b"x")
    assert voice.model_ready(root, "small") is True
    assert voice.model_ready(root, "base") is False
    assert voice.model_ready(tmp_path / "пусто", "small") is False


def test_model_by_full_path_is_seen(tmp_path):
    """Модель могут положить папкой целиком — путь в настройках вместо имени."""
    folder = tmp_path / "своя-модель"
    folder.mkdir()
    (folder / "model.bin").write_bytes(b"x")
    assert voice.model_ready(tmp_path, str(folder)) is True


# --- подставные движки для остальных тестов --------------------------------

def test_fake_transcriber_is_available_and_speaks_russian(tmp_path):
    t = voice.FakeTranscriber(text="Собери отчёт")
    assert t.available() is True
    assert t.transcribe(tmp_path / "нет.ogg") == "Собери отчёт"


def test_fake_transcriber_can_pretend_there_is_no_library(tmp_path):
    t = voice.FakeTranscriber(available=False)
    assert t.available() is False
    with pytest.raises(voice.VoiceNotSetUp):
        t.transcribe(tmp_path / "нет.ogg")


def test_fake_speaker_writes_a_file(tmp_path):
    s = voice.FakeSpeaker()
    path = s.say("Отчёт готов", tmp_path)
    assert path.exists()
    assert path.suffix == ".wav"
    assert s.said == ["Отчёт готов"]


def test_speaker_without_piper_is_not_available(tmp_path):
    s = voice.PiperSpeaker(model_path=tmp_path / "нет.onnx")
    assert s.available() is False
    with pytest.raises(voice.VoiceNotSetUp):
        s.say("привет", tmp_path)


def test_piper_voice_file_is_looked_for_by_name(tmp_path):
    (tmp_path / "ru_RU-irina-medium.onnx").write_bytes(b"onnx")
    s = voice.PiperSpeaker(voice_name="ru_RU-irina-medium", voices_dir=tmp_path,
                           runner=lambda *a, **k: None)
    assert s.model_ready() is True

    other = voice.PiperSpeaker(voice_name="ru_RU-dmitri-medium", voices_dir=tmp_path,
                               runner=lambda *a, **k: None)
    assert other.model_ready() is False


def test_piper_runs_the_binary_and_returns_the_wav(tmp_path):
    (tmp_path / "ru_RU-irina-medium.onnx").write_bytes(b"onnx")
    calls = []

    def runner(text, model_path, out_path):
        calls.append((text, str(model_path), str(out_path)))
        out_path.write_bytes(b"RIFFwav")

    s = voice.PiperSpeaker(voice_name="ru_RU-irina-medium", voices_dir=tmp_path,
                           runner=runner)
    assert s.available() is True
    path = s.say("Отчёт готов", tmp_path)
    assert path.read_bytes() == b"RIFFwav"
    assert calls[0][0] == "Отчёт готов"


def test_piper_does_not_speak_more_than_the_limit(tmp_path):
    (tmp_path / "ru_RU-irina-medium.onnx").write_bytes(b"onnx")
    said = []

    def runner(text, model_path, out_path):
        said.append(text)
        out_path.write_bytes(b"RIFFwav")

    s = voice.PiperSpeaker(voice_name="ru_RU-irina-medium", voices_dir=tmp_path,
                           runner=runner)
    s.say("б" * 5000, tmp_path)
    assert len(said[0]) <= voice.MAX_SPEAK_CHARS


# --- конвертация в ogg/opus -------------------------------------------------

def test_wav_becomes_ogg_when_ffmpeg_is_there(tmp_path):
    wav = tmp_path / "ответ.wav"
    wav.write_bytes(b"RIFF")
    calls = []

    def runner(src, dst):
        calls.append((str(src), str(dst)))
        dst.write_bytes(b"OggS")
        return True

    out = voice.to_ogg(wav, runner=runner)
    assert out is not None and out.suffix == ".ogg"
    assert out.read_bytes() == b"OggS"


def test_without_ffmpeg_wav_stays_wav(tmp_path):
    wav = tmp_path / "ответ.wav"
    wav.write_bytes(b"RIFF")
    assert voice.to_ogg(wav, runner=lambda src, dst: False) is None


# --- сборка по настройкам ---------------------------------------------------

def test_build_gives_working_pair_from_config(config):
    ears, mouth = voice.build(config)
    assert isinstance(ears, voice.Transcriber)
    assert isinstance(mouth, voice.Speaker)


def test_build_respects_the_switch_off(config):
    config.voice.enabled = False
    ears, mouth = voice.build(config)
    assert ears.available() is False
    assert mouth.available() is False


# --- модель слуха не живёт в памяти вечно (замер сервера 15.09) --------------

class ПоддельнаяМодель:
    def transcribe(self, path, **kwargs):
        class Инфо:
            duration = 3.0
        return [], Инфо()


def подъёмник(счётчик):
    def loader():
        счётчик.append(1)
        return ПоддельнаяМодель()
    return loader


def test_the_model_is_let_go_after_a_long_quiet(tmp_path):
    """550 МБ против 37 — столько стоит модель, которую держат вечно."""
    from bridge.voice import WhisperTranscriber

    счётчик = []
    ears = WhisperTranscriber(loader=подъёмник(счётчик))
    ears.transcribe(tmp_path / "zapis.ogg")
    assert счётчик == [1]

    assert ears.release_if_idle(after_sec=1800, now=time.monotonic()) is False   # ещё рано
    assert ears.release_if_idle(after_sec=1800, now=time.monotonic() + 1801) is True
    assert ears._model is None


def test_it_comes_back_by_itself_on_the_next_voice(tmp_path):
    """Отпустили — и человек этого не замечает, кроме пары секунд ожидания."""
    from bridge.voice import WhisperTranscriber

    счётчик = []
    ears = WhisperTranscriber(loader=подъёмник(счётчик))
    ears.transcribe(tmp_path / "raz.ogg")
    ears.release()
    ears.transcribe(tmp_path / "dva.ogg")
    assert счётчик == [1, 1]                       # поднялась второй раз, сама


def test_a_busy_bridge_does_not_lose_its_model(tmp_path):
    """Простой считается от конца работы, а не от начала."""
    from bridge.voice import WhisperTranscriber

    ears = WhisperTranscriber(loader=подъёмник([]))
    ears.transcribe(tmp_path / "raz.ogg")
    assert ears.release_if_idle(after_sec=60, now=time.monotonic() + 30) is False


def test_zero_means_never_let_it_go(tmp_path):
    """Кому память не жалко, тот платит секундами — и это его право."""
    from bridge.voice import WhisperTranscriber

    ears = WhisperTranscriber(loader=подъёмник([]))
    ears.transcribe(tmp_path / "raz.ogg")
    assert ears.release_if_idle(after_sec=0, now=time.monotonic() + 100000) is False
    assert ears._model is not None


def test_the_memory_goes_back_to_the_system_not_just_to_python(tmp_path):
    """Сборщик мусора вернул пятую часть, `malloc_trim` — остальное.

    Замер на сервере 15.09: 528 МБ до отпускания, 423 после `gc.collect()`
    и 160 после `malloc_trim`. Без этого вызова обещание «память вернулась»
    было бы почти неправдой.
    """
    from bridge.voice import WhisperTranscriber

    ears = WhisperTranscriber(loader=подъёмник([]))
    ears.transcribe(tmp_path / "raz.ogg")

    позвали = []

    class Libc:
        def malloc_trim(self, сколько):
            позвали.append(сколько)

    import bridge.voice as voice_module
    было = voice_module.vernut_pamyat
    voice_module.vernut_pamyat = lambda loader=None: было(loader=lambda: Libc())
    try:
        assert ears.release() is True
    finally:
        voice_module.vernut_pamyat = было
    assert позвали == [0]


def test_a_machine_without_malloc_trim_does_not_break(tmp_path):
    """Макбук и musl-контейнеры этого вызова не знают — и это не беда."""
    from bridge.voice import vernut_pamyat

    def нет_такого():
        raise OSError("libc.so.6: cannot open shared object file")

    assert vernut_pamyat(loader=нет_такого) is False


def test_there_is_nothing_to_let_go_when_the_voice_is_not_set_up():
    from bridge.voice import Transcriber

    глухой = Transcriber()
    assert глухой.release() is False
    assert глухой.release_if_idle(after_sec=1) is False


def test_the_setting_is_read_and_zero_survives(home):
    """`0` — это «никогда», а не «не сказано»: его нельзя подменять умолчанием."""
    from bridge.config import load_config

    (home / "config.yaml").write_text(
        'telegram:\n  token: "t"\n  allowlist: []\nvoice:\n  unload_after_min: 0\n',
        encoding="utf-8")
    assert load_config(home=home, name="test").voice.unload_after_min == 0

    (home / "config.yaml").write_text(
        'telegram:\n  token: "t"\n  allowlist: []\nvoice:\n  unload_after_min: 5\n',
        encoding="utf-8")
    assert load_config(home=home, name="test").voice.unload_after_min == 5

    (home / "config.yaml").write_text(
        'telegram:\n  token: "t"\n  allowlist: []\n', encoding="utf-8")
    assert load_config(home=home, name="test").voice.unload_after_min == 30
