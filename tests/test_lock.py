"""Замок экземпляра: один мост на одно имя, и соседи по машине.

Причина третья из ворот 2 («этого бота уже слушает кто-то ещё») начинается
здесь: второй мост того же экземпляра не должен подниматься вовсе.
"""
import os

from bridge import lock


def test_the_second_bridge_does_not_get_the_lock(home):
    first = lock.InstanceLock(home / "most.lock")
    assert first.acquire() is True
    second = lock.InstanceLock(home / "most.lock")
    assert second.acquire() is False
    assert second.holder_pid() == os.getpid()
    first.release()
    assert second.acquire() is True
    second.release()


def test_the_lock_file_keeps_the_number_of_the_process(home):
    with lock.InstanceLock(home / "most.lock") as held:
        assert held is True
        assert (home / "most.lock").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_a_free_lock_has_no_holder(home):
    assert lock.InstanceLock(home / "most.lock").holder_pid() is None


def test_alive_tells_a_living_process_from_a_dead_one():
    assert lock.alive(os.getpid()) is True
    assert lock.alive(0) is False
    assert lock.alive(None) is False
    assert lock.alive(4_000_000) is False


def test_the_command_of_a_process_is_readable():
    assert "python" in (lock.command_of(os.getpid()) or "").lower()


def test_a_stranger_process_is_not_taken_for_our_neural_net():
    """Номера процессов переиспользуются: гасим только то, что и правда claude."""
    assert lock.looks_like_claude(os.getpid()) is False
    assert lock.looks_like_claude(os.getpid(),
                                  command="/home/u/.local/bin/claude -p сделай") is True


def test_the_second_bridge_of_the_same_name_is_found_among_the_processes():
    lines = [
        "  501 /usr/bin/python3 -m bridge --name anna",
        "  777 /usr/bin/python3 -m bridge --name boris",
        "  888 grep bridge",
    ]
    assert lock.other_bridges("anna", mine=999, lines=lines) == [(501, lines[0].strip())]
    assert lock.other_bridges("anna", mine=501, lines=lines) == []
    assert lock.other_bridges("boris", mine=1, lines=lines) == [(777, lines[1].strip())]
