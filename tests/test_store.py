"""Хранилище: схема заводится сразу под все этапы, состояние переживает рестарт."""
from bridge.store import Store


def test_all_tables_exist_from_the_first_start(store):
    names = store.table_names()
    for table in ("links", "schedule", "allowlist", "settings", "journal", "jobs"):
        assert table in names


def test_wal_is_on(store):
    assert store.journal_mode().lower() == "wal"


def test_settings_survive_reopen(home):
    s = Store(home / "most.db")
    s.init()
    s.set_setting("telegram_offset", "42")
    s.close()

    again = Store(home / "most.db")
    again.init()
    assert again.get_setting("telegram_offset") == "42"
    assert again.get_setting("max_marker") is None
    assert again.get_setting("max_marker", "0") == "0"
    again.close()


def test_offset_and_marker_are_kept_apart(store):
    store.set_setting("telegram_offset", "10")
    store.set_setting("max_marker", "998877")
    assert store.get_setting("telegram_offset") == "10"
    assert store.get_setting("max_marker") == "998877"


def test_link_is_created_once_per_chat(store):
    first = store.upsert_link("telegram", 500, 0, project="buhgalter", session_id="s-1")
    same = store.get_link("telegram", 500, 0)
    assert same["id"] == first["id"]
    assert same["project"] == "buhgalter"
    assert same["session_id"] == "s-1"


def test_link_project_can_be_switched_and_session_kept(store):
    store.upsert_link("telegram", 500, 0, project="buhgalter", session_id="s-1")
    store.set_link_project("telegram", 500, 0, "analitika", session_id="s-2")
    link = store.get_link("telegram", 500, 0)
    assert link["project"] == "analitika"
    assert link["session_id"] == "s-2"


def test_links_of_two_channels_do_not_collide(store):
    store.upsert_link("telegram", 7, 0, project="buhgalter", session_id="s-tg")
    store.upsert_link("max", 7, 0, project="analitika", session_id="s-max")
    assert store.get_link("telegram", 7, 0)["project"] == "buhgalter"
    assert store.get_link("max", 7, 0)["project"] == "analitika"


def test_allowlist_from_config_replaces_previous(store):
    store.sync_allowlist("telegram", [1, 2, 3])
    assert store.is_allowed("telegram", 2) is True
    store.sync_allowlist("telegram", [9])
    assert store.is_allowed("telegram", 2) is False
    assert store.is_allowed("telegram", 9) is True


def test_empty_allowlist_allows_nobody(store):
    store.sync_allowlist("telegram", [])
    assert store.is_allowed("telegram", 1) is False


def test_allowlist_is_per_channel(store):
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    assert store.is_allowed("max", 111) is False
    assert store.is_allowed("max", 222) is True


def test_journal_keeps_strangers_with_first_40_chars(store):
    long_text = "а" * 200
    store.note_stranger("telegram", chat_id=5, user_id=9, text=long_text)
    knocks = store.recent_strangers(limit=10)
    assert len(knocks) == 1
    assert knocks[0]["user_id"] == 9
    assert knocks[0]["channel"] == "telegram"
    assert len(knocks[0]["text"]) <= 40
    assert knocks[0]["at"]


def test_recent_strangers_are_newest_first_and_limited(store):
    for i in range(5):
        store.note_stranger("telegram", chat_id=1, user_id=i, text=f"стук {i}")
    knocks = store.recent_strangers(limit=3)
    assert [k["user_id"] for k in knocks] == [4, 3, 2]


def test_job_row_is_written_and_finished(store):
    link = store.upsert_link("telegram", 1, 0, project="buhgalter", session_id="s")
    job_id = store.start_job(link_id=link["id"], channel="telegram", chat_id=1,
                             session_id="s", prompt="посчитай остатки", job_dir="/tmp/x")
    store.finish_job(job_id, state="done", exit_code=0)
    row = store.get_job(job_id)
    assert row["state"] == "done"
    assert row["exit_code"] == 0
    assert row["finished_at"]


def test_schedule_table_accepts_a_row(store):
    """Расписание — этап 5, но таблица обязана быть с первого дня: миграции
    на ученической машине дороже пустых таблиц."""
    link = store.upsert_link("telegram", 1, 0, project="buhgalter", session_id="s")
    store.add_schedule(link_id=link["id"], spec="07:00", prompt="собери сводку")
    rows = store.list_schedule()
    assert rows[0]["spec"] == "07:00"
    assert rows[0]["enabled"] == 1
