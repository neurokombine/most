"""«Покажи, что получилось»: мост сам смотрит на папку, без нейросети."""
import os
import time

from bridge.changes import changed_files


def touch(path, when=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if when is not None:
        os.utime(path, (when, when))
    return path


def test_only_files_newer_than_the_mark_are_shown(tmp_path):
    old = time.time() - 3600
    touch(tmp_path / "staroe.txt", when=old)
    touch(tmp_path / "otchet.md")

    found = changed_files(tmp_path, since=time.time() - 60)
    names = [name for name, _ in found]
    assert names == ["otchet.md"]


def test_newest_first_and_no_more_than_the_limit(tmp_path):
    now = time.time()
    for n in range(30):
        touch(tmp_path / f"file{n:02d}.txt", when=now - n)

    found = changed_files(tmp_path, since=now - 3600, limit=20)
    assert len(found) == 20
    assert found[0][0] == "file00.txt"


def test_service_folders_are_not_shown(tmp_path):
    now = time.time()
    touch(tmp_path / ".git" / "HEAD")
    touch(tmp_path / "__pycache__" / "x.pyc")
    touch(tmp_path / ".venv" / "bin" / "python")
    touch(tmp_path / "node_modules" / "left-pad" / "index.js")
    touch(tmp_path / "otchet.md")

    names = [name for name, _ in changed_files(tmp_path, since=now - 3600)]
    assert names == ["otchet.md"]


def test_subfolders_are_shown_with_their_path(tmp_path):
    now = time.time()
    touch(tmp_path / "rezultaty" / "avgust.csv")
    names = [name for name, _ in changed_files(tmp_path, since=now - 3600)]
    assert names == ["rezultaty/avgust.csv"]


def test_missing_folder_is_empty_not_a_crash(tmp_path):
    assert changed_files(tmp_path / "net-takoy-papki", since=0) == []
