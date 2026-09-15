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


# --- этап 2: сессия связки, восстановление после перезагрузки, история работ ----

def test_session_is_marked_as_started_only_after_the_first_work(store):
    link = store.upsert_link("telegram", 500, 0, project="buhgalter", session_id="s-1")
    assert link["session_started"] == 0
    store.mark_session_started(link["id"])
    assert store.get_link("telegram", 500, 0)["session_started"] == 1


def test_new_session_replaces_the_old_key_and_forgets_that_it_was_started(store):
    link = store.upsert_link("telegram", 500, 0, project="buhgalter", session_id="s-1")
    store.mark_session_started(link["id"])
    store.reset_session(link["id"], "s-2")
    again = store.get_link("telegram", 500, 0)
    assert again["session_id"] == "s-2"
    assert again["session_started"] == 0


def test_old_base_without_the_new_columns_is_upgraded_not_broken(home):
    import sqlite3
    path = home / "most.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE links (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT,
            chat_id INTEGER, thread_id INTEGER DEFAULT 0, project TEXT, session_id TEXT,
            title TEXT, created_at TEXT, last_job_at TEXT, state TEXT DEFAULT 'idle');
        INSERT INTO links(channel, chat_id, thread_id, project, session_id, created_at)
            VALUES('telegram', 500, 0, 'buhgalter', 's-1', '2026-09-15T00:00:00+00:00');
    """)
    db.commit()
    db.close()

    s = Store(path).init()
    link = s.get_link("telegram", 500, 0)
    assert link["project"] == "buhgalter"          # старые данные на месте
    assert link["session_started"] == 0            # новая колонка появилась
    s.close()


def test_running_jobs_become_interrupted_after_a_restart(store):
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    job_id = store.start_job(link["id"], "telegram", 500, "s-1",
                             "посчитай остатки за август", "")
    left = store.mark_running_interrupted()
    assert [row["id"] for row in left] == [job_id]
    assert store.get_job(job_id)["state"] == "interrupted"
    # второй запуск уже ничего не находит — сообщение не повторяется
    assert store.mark_running_interrupted() == []


def test_finished_job_keeps_how_long_it_took_and_what_came_out(store):
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    job_id = store.start_job(link["id"], "telegram", 500, "s-1", "посчитай", "")
    store.set_job_dir(job_id, "/tmp/jobs/1")
    store.finish_job(job_id, "done", exit_code=0, duration_sec=12.5, result_head="Остатки: 17")
    row = store.get_job(job_id)
    assert row["duration_sec"] == 12.5
    assert row["result_head"] == "Остатки: 17"
    assert row["dir"] == "/tmp/jobs/1"


def test_link_history_is_newest_first_and_limited(store):
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    other = store.upsert_link("max", 900, 0, project="analitika")
    for n in range(7):
        job_id = store.start_job(link["id"], "telegram", 500, "s-1", f"задача {n}", "")
        store.finish_job(job_id, "done", exit_code=0, duration_sec=n)
    store.start_job(other["id"], "max", 900, "s-2", "чужая задача", "")

    rows = store.jobs_of_link(link["id"], limit=5)
    assert len(rows) == 5
    assert rows[0]["prompt_head"] == "задача 6"
    assert all("чужая" not in r["prompt_head"] for r in rows)


def test_last_finished_job_of_a_link_skips_the_running_one(store):
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    done = store.start_job(link["id"], "telegram", 500, "s-1", "первая", "")
    store.finish_job(done, "done", exit_code=0)
    store.start_job(link["id"], "telegram", 500, "s-1", "вторая, ещё идёт", "")
    assert store.last_finished_job(link["id"])["id"] == done


# --- этап 5: расписание живёт в базе ---------------------------------------

def test_schedule_keeps_folder_time_and_next_run(store):
    """Задача привязана к папке, в которой её задали: связку потом переключат,
    а задача должна остаться там же."""
    link = store.upsert_link("telegram", 1, 0, project="buhgalter", session_id="s")
    task_id = store.add_schedule(link_id=link["id"], spec="daily 07:30",
                                 prompt="собери сводку", project="buhgalter",
                                 next_run_at="2026-09-16T04:30:00+00:00")
    row = store.get_schedule(task_id)
    assert row["project"] == "buhgalter"
    assert row["next_run_at"] == "2026-09-16T04:30:00+00:00"
    assert row["last_status"] is None


def test_schedule_can_be_moved_switched_off_and_removed(store):
    link = store.upsert_link("telegram", 1, 0, project="buhgalter", session_id="s")
    task_id = store.add_schedule(link["id"], "daily 07:30", "сводка", project="buhgalter")

    store.set_schedule_spec(task_id, "daily 08:00", next_run_at="2026-09-16T05:00:00+00:00")
    assert store.get_schedule(task_id)["spec"] == "daily 08:00"

    store.enable_schedule(task_id, False)
    assert store.list_schedule(only_enabled=True) == []

    store.remove_schedule(task_id)
    assert store.get_schedule(task_id) is None


def test_schedule_remembers_how_the_run_ended(store):
    link = store.upsert_link("telegram", 1, 0, project="buhgalter", session_id="s")
    task_id = store.add_schedule(link["id"], "daily 07:30", "сводка", project="buhgalter")
    store.mark_schedule_run(task_id, status="done", at="2026-09-16T04:30:00+00:00",
                            next_run_at="2026-09-17T04:30:00+00:00")
    row = store.get_schedule(task_id)
    assert row["last_status"] == "done"
    assert row["last_run_at"] == "2026-09-16T04:30:00+00:00"
    assert row["next_run_at"] == "2026-09-17T04:30:00+00:00"


def test_job_keeps_the_price_and_the_task_it_came_from(store):
    """Цену работы надо где-то держать: из неё складывается «сколько потрачено»."""
    link = store.upsert_link("telegram", 1, 0, project="buhgalter", session_id="s")
    task_id = store.add_schedule(link["id"], "daily 07:30", "сводка", project="buhgalter")
    job_id = store.start_job(link_id=link["id"], channel="telegram", chat_id=1,
                             session_id="s", prompt="сводка", job_dir="")
    store.set_job_schedule(job_id, task_id)
    store.finish_job(job_id, state="done", exit_code=0, duration_sec=12.0, cost_usd=0.21)

    row = store.get_job(job_id)
    assert row["schedule_id"] == task_id
    assert row["cost_usd"] == 0.21


def test_jobs_since_sees_only_the_last_day_and_only_scheduled_ones(store):
    link = store.upsert_link("telegram", 1, 0, project="buhgalter", session_id="s")
    task_id = store.add_schedule(link["id"], "daily 07:30", "сводка", project="buhgalter")

    old = store.start_job(link["id"], "telegram", 1, "s", "позавчерашняя", "")
    store.db.execute("UPDATE jobs SET started_at=? WHERE id=?",
                     ("2026-09-01T00:00:00+00:00", old))
    by_hand = store.start_job(link["id"], "telegram", 1, "s", "руками", "")
    by_alarm = store.start_job(link["id"], "telegram", 1, "s", "по расписанию", "")
    store.set_job_schedule(by_alarm, task_id)

    since = "2026-09-02T00:00:00+00:00"
    assert {r["id"] for r in store.jobs_since(since)} == {by_hand, by_alarm}
    assert [r["id"] for r in store.jobs_since(since, only_scheduled=True)] == [by_alarm]
    assert store.jobs_since("2027-01-01T00:00:00+00:00") == []


def test_money_spent_is_counted_only_where_the_price_is_known(store):
    link = store.upsert_link("telegram", 1, 0, project="buhgalter", session_id="s")
    first = store.start_job(link["id"], "telegram", 1, "s", "раз", "")
    second = store.start_job(link["id"], "telegram", 1, "s", "два", "")
    store.finish_job(first, state="done", cost_usd=0.12)
    store.finish_job(second, state="done")                 # цену нейросеть не назвала

    spent, known = store.spent_since("2026-01-01T00:00:00+00:00")
    assert round(spent, 2) == 0.12
    assert known == 1


def test_strangers_and_journal_are_readable_by_day(store):
    store.note_stranger("telegram", 5, 555, "привет")
    store.note("missed", text="задача 2 пропущена")
    since = "2026-01-01T00:00:00+00:00"
    assert len(store.strangers_since(since)) == 1
    assert len(store.journal_since("missed", since)) == 1
    assert store.journal_since("missed", "2027-01-01T00:00:00+00:00") == []


# --- этап 6: белый список руками, имя стучавшегося, pid работы ---------------

def test_allowlist_can_be_changed_without_the_config(store):
    """Нейросеть пускает человека командой — и мост слышит это без перезапуска."""
    assert store.is_allowed("telegram", 555) is False
    assert store.allow("telegram", 555, note="пустила по просьбе") is True
    assert store.is_allowed("telegram", 555) is True
    assert store.allow("telegram", 555) is False          # второй раз — уже свой
    assert store.deny("telegram", 555) is True
    assert store.is_allowed("telegram", 555) is False
    assert store.deny("telegram", 555) is False           # убирать больше нечего


def test_allowlist_is_listed_with_channels(store):
    store.allow("telegram", 111)
    store.allow("max", 222)
    rows = store.list_allowed()
    assert {(r["channel"], r["user_id"]) for r in rows} == {("telegram", 111), ("max", 222)}
    assert [r["user_id"] for r in store.list_allowed("max")] == [222]


def test_the_knock_remembers_the_name(store):
    store.note_stranger("telegram", 10, 555, "привет", name="Наталья")
    row = store.recent_strangers()[0]
    assert row["user_id"] == 555
    assert row["name"] == "Наталья"
    assert row["text"] == "привет"


def test_the_knock_without_a_name_is_fine(store):
    store.note_stranger("max", 10, 777, "привет")
    assert store.recent_strangers()[0]["name"] in (None, "")


def test_the_last_knock_of_every_channel(store):
    store.note_stranger("telegram", 10, 111, "раз")
    store.note_stranger("telegram", 10, 222, "два")
    store.note_stranger("max", 10, 333, "три")
    last = store.last_knocks()
    assert last == {"telegram": 222, "max": 333}


def test_a_running_job_remembers_its_pid(store):
    job_id = store.start_job(link_id=None, channel="telegram", chat_id=1,
                             session_id="s", prompt="сделай", job_dir="")
    store.set_job_pid(job_id, 4242)
    rows = store.running_jobs()
    assert [(r["id"], r["pid"]) for r in rows] == [(job_id, 4242)]
    assert store.mark_running_interrupted()[0]["pid"] == 4242
    assert store.running_jobs() == []
