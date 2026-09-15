"""Порог памяти под голос: ставим его по умолчанию или честно говорим, что нет.

Решение 3 плана сборки плюс находка приёмки 15.09: мост, обещавший голосовое
и ответивший «повторите текстом», для человека сломан. Значит, либо голос
стоит, либо человеку сказано, почему его нет.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pamyat


GIB = 1024 ** 3


def meminfo(gib: float) -> str:
    return f"MemTotal:       {int(gib * GIB / 1024)} kB\nMemFree:         100000 kB\n"


def test_eight_gigabytes_are_enough():
    assert pamyat.enough_for_voice(pamyat.total_memory(meminfo(8))) is True


def test_four_gigabytes_are_not():
    """Машина из программы курса — ровно этот случай."""
    assert pamyat.enough_for_voice(pamyat.total_memory(meminfo(4))) is False


def test_six_gigabytes_are_the_border_and_the_border_counts():
    assert pamyat.enough_for_voice(pamyat.total_memory(meminfo(6))) is True
    assert pamyat.enough_for_voice(pamyat.total_memory(meminfo(5.9))) is False


def test_memory_is_read_in_bytes_not_in_kilobytes():
    assert abs(pamyat.total_memory(meminfo(8)) - 8 * GIB) < GIB / 100


def test_an_unmeasurable_machine_is_not_left_without_a_voice():
    """Неудавшийся замер — не повод молча лишать человека голоса."""
    assert pamyat.total_memory("ничего похожего") is None
    assert pamyat.enough_for_voice(None) is True


def test_the_script_answers_with_a_code_so_that_a_shell_can_ask_it():
    assert pamyat.main(["--tiho"]) in (0, 1)


def test_it_leans_on_nothing_but_the_standard_library():
    """Его зовёт setup.sh системным питоном — до того, как встало окружение."""
    text = (Path(pamyat.__file__)).read_text(encoding="utf-8")
    for чужое in ("import requests", "import yaml", "from bridge", "faster_whisper"):
        assert чужое not in text
