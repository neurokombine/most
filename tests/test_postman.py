"""Почтальон: куда кладём присланное, как ищем файл по имени, как называем размер."""
import pytest

from bridge import postman


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "буhgalter"          # кириллица в пути — так бывает у ученика
    d.mkdir()
    return d


# --- входящие ---------------------------------------------------------------

def test_incoming_file_lands_in_russian_folder(workdir):
    path = postman.save_incoming(workdir, "отчёт.xlsx", b"12345")
    assert path.parent.name == postman.INBOX
    assert path.parent.name == "входящие"
    assert path.name == "отчёт.xlsx"
    assert path.read_bytes() == b"12345"


def test_folder_with_cyrillic_name_is_created_once(workdir):
    postman.save_incoming(workdir, "раз.txt", b"a")
    postman.save_incoming(workdir, "два.txt", b"b")
    assert sorted(p.name for p in (workdir / postman.INBOX).iterdir()) == ["два.txt", "раз.txt"]


def test_same_name_twice_does_not_overwrite(workdir):
    first = postman.save_incoming(workdir, "смета.pdf", "один".encode("utf-8"))
    second = postman.save_incoming(workdir, "смета.pdf", "два".encode("utf-8"))
    assert first != second
    assert first.read_bytes() == "один".encode("utf-8")
    assert second.read_bytes() == "два".encode("utf-8")
    assert second.name.startswith("смета") and second.name.endswith(".pdf")


def test_spaces_in_name_survive(workdir):
    path = postman.save_incoming(workdir, "акт сверки за сентябрь.pdf", b"x")
    assert path.name == "акт сверки за сентябрь.pdf"


def test_name_with_slashes_cannot_escape_the_folder(workdir):
    path = postman.save_incoming(workdir, "../../побег.txt", b"x")
    assert path.parent == workdir / postman.INBOX
    assert ".." not in path.name


def test_nameless_file_gets_a_name_with_date(workdir):
    path = postman.save_incoming(workdir, "", b"x", kind="photo")
    assert path.suffix == ".jpg"
    assert path.parent.name == postman.INBOX
    assert path.name != ".jpg"


def test_kind_decides_the_extension_when_there_is_none(workdir):
    assert postman.save_incoming(workdir, "", b"x", kind="video").suffix == ".mp4"
    assert postman.save_incoming(workdir, "", b"x", kind="audio").suffix == ".m4a"
    assert postman.save_incoming(workdir, "", b"x", kind="file").suffix == ".bin"


# --- размеры ----------------------------------------------------------------

@pytest.mark.parametrize("size, said", [
    (0, "0 байт"),
    (900, "900 байт"),
    (2048, "2 КБ"),
    (1024 * 1024 * 3 + 512 * 1024, "3,5 МБ"),
    (1024 * 1024 * 21, "21 МБ"),
])
def test_size_is_said_in_russian(size, said):
    assert postman.human_size(size) == said


# --- поиск файла по имени ---------------------------------------------------

def make_files(root, *names):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


def test_finds_by_part_of_the_name_without_case(workdir):
    make_files(workdir, "Отчёт-Сентябрь.xlsx", "смета.pdf")
    found = postman.find_files(workdir, "отчёт")
    assert [p.name for p in found] == ["Отчёт-Сентябрь.xlsx"]


def test_finds_by_several_words_in_any_order(workdir):
    make_files(workdir, "сентябрь-отчёт-итог.xlsx")
    assert postman.find_files(workdir, "отчёт сентябрь")


def test_several_matches_come_back_all(workdir):
    make_files(workdir, "отчёт-август.xlsx", "отчёт-сентябрь.xlsx")
    found = postman.find_files(workdir, "отчёт")
    assert len(found) == 2


def test_nothing_found_is_an_empty_list(workdir):
    make_files(workdir, "смета.pdf")
    assert postman.find_files(workdir, "накладная") == []


def test_service_folders_are_not_searched(workdir):
    make_files(workdir, ".git/отчёт.xlsx", "node_modules/отчёт.js", "отчёт.xlsx")
    found = postman.find_files(workdir, "отчёт")
    assert [p.name for p in found] == ["отчёт.xlsx"]


def test_file_in_a_subfolder_is_found_too(workdir):
    make_files(workdir, "отчёты/сентябрь/итог.xlsx")
    found = postman.find_files(workdir, "итог")
    assert len(found) == 1
    assert found[0].name == "итог.xlsx"


def test_extension_in_the_question_works(workdir):
    make_files(workdir, "смета.pdf", "смета.xlsx")
    found = postman.find_files(workdir, "смета.pdf")
    assert [p.name for p in found] == ["смета.pdf"]


def test_yo_and_ye_are_the_same_letter(workdir):
    make_files(workdir, "отчет-сентябрь.xlsx")
    assert postman.find_files(workdir, "отчёт")


def test_recent_files_are_newest_first(workdir):
    import os
    make_files(workdir, "старый.txt", "новый.txt")
    os.utime(workdir / "старый.txt", (1_600_000_000, 1_600_000_000))
    names = [p.name for p in postman.recent_files(workdir, limit=5)]
    assert names[0] == "новый.txt"


def test_recent_files_of_a_bare_folder_are_empty(tmp_path):
    assert postman.recent_files(tmp_path / "нет-такой", limit=5) == []
