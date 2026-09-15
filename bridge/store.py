"""SQLite моста: всё состояние на диске, ничего в памяти демона.

Схема заводится целиком с первого запуска — включая таблицы будущих этапов
(расписание, работы). Пустая таблица ничего не стоит, а миграция на
ученической машине стоит вечера.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT    NOT NULL,
    chat_id       INTEGER NOT NULL,
    thread_id     INTEGER NOT NULL DEFAULT 0,
    project       TEXT,
    session_id    TEXT,
    title         TEXT,
    created_at    TEXT    NOT NULL,
    last_job_at   TEXT,
    state         TEXT    NOT NULL DEFAULT 'idle',
    session_started INTEGER NOT NULL DEFAULT 0,
    UNIQUE (channel, chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS schedule (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id       INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    spec          TEXT    NOT NULL,
    prompt        TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL,
    last_run_at   TEXT,
    last_status   TEXT,
    project       TEXT,
    next_run_at   TEXT
);

CREATE TABLE IF NOT EXISTS allowlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT    NOT NULL,
    user_id       INTEGER NOT NULL,
    note          TEXT,
    added_at      TEXT    NOT NULL,
    UNIQUE (channel, user_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key           TEXT PRIMARY KEY,
    value         TEXT
);

CREATE TABLE IF NOT EXISTS journal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    channel       TEXT,
    chat_id       INTEGER,
    user_id       INTEGER,
    name          TEXT,
    text          TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    link_id       INTEGER REFERENCES links(id) ON DELETE SET NULL,
    channel       TEXT,
    chat_id       INTEGER,
    session_id    TEXT,
    prompt_head   TEXT,
    dir           TEXT,
    state         TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    exit_code     INTEGER,
    duration_sec  REAL,
    result_head   TEXT,
    pid           INTEGER
);

CREATE INDEX IF NOT EXISTS journal_at ON journal (at DESC);
CREATE INDEX IF NOT EXISTS jobs_started ON jobs (started_at DESC);
"""

KNOCK_TEXT_LIMIT = 40
KNOCK_NAME_LIMIT = 60
RESULT_HEAD_LIMIT = 200

# Колонки, которые появились позже первой версии. База у ученика уже живёт,
# и ронять её ради нового поля нельзя: добираем недостающее на месте.
LATE_COLUMNS = {
    "links": [("session_started", "INTEGER NOT NULL DEFAULT 0")],
    "jobs": [("duration_sec", "REAL"), ("result_head", "TEXT"),
             ("cost_usd", "REAL"), ("schedule_id", "INTEGER"), ("pid", "INTEGER")],
    "journal": [("name", "TEXT")],
    "schedule": [("project", "TEXT"), ("next_run_at", "TEXT")],
}

# Состояния работы. Живая одна, остальные — чем всё кончилось.
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_TIMEOUT = "timeout"
JOB_STOPPED = "stopped"
JOB_INTERRUPTED = "interrupted"
JOB_FINISHED_STATES = (JOB_DONE, JOB_FAILED, JOB_TIMEOUT, JOB_STOPPED, JOB_INTERRUPTED)


class _Rows:
    """Готовый ответ базы: строки уже вынуты, соединение больше не нужно."""

    def __init__(self, rows, lastrowid):
        self._rows = rows
        self.lastrowid = lastrowid

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _SafeConn:
    """Соединение под замком: к базе ходят и главный цикл, и потоки работ.

    Замок держим на всё время запроса и достаём строки сразу: иначе закрытие
    базы в один поток посреди чужого запроса роняет процесс целиком
    (получили segfault на первом же прогоне, это не теория).
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def execute(self, sql: str, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            try:
                rows = cur.fetchall()
            except sqlite3.Error:
                rows = []
            return _Rows(rows, cur.lastrowid)

    def executescript(self, sql: str):
        with self._lock:
            return self._conn.executescript(sql)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def now_iso() -> str:
    """Время в UTC со смещением. Показываем человеку — переводим в московское."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._db: _SafeConn | None = None
        self._lock = threading.RLock()

    # --- служебное ----------------------------------------------------------

    @property
    def db(self) -> _SafeConn:
        with self._lock:
            if self._db is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # check_same_thread=False: работы идут в отдельных потоках, и
                # итог пишет тот поток, который её делал; очередь к базе держит
                # замок в _SafeConn.
                conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                       check_same_thread=False)
                conn.row_factory = sqlite3.Row
                self._db = _SafeConn(conn, self._lock)
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA foreign_keys=ON")
                self._db.execute("PRAGMA busy_timeout=5000")
            return self._db

    def init(self) -> "Store":
        self.db.executescript(SCHEMA)
        self._add_late_columns()
        return self

    def _add_late_columns(self) -> None:
        """Догоняем базу, заведённую прошлой версией моста: ALTER вместо переезда."""
        for table, columns in LATE_COLUMNS.items():
            if table not in self.table_names():
                continue
            have = {row["name"] for row in
                    self.db.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, kind in columns:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def table_names(self) -> list[str]:
        rows = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [r["name"] for r in rows]

    def journal_mode(self) -> str:
        return self.db.execute("PRAGMA journal_mode").fetchone()[0]

    # --- настройки (offset Telegram, marker Max) ----------------------------

    def get_setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    # --- связки -------------------------------------------------------------

    def get_link(self, channel: str, chat_id: int, thread_id: int = 0):
        return self.db.execute(
            "SELECT * FROM links WHERE channel=? AND chat_id=? AND thread_id=?",
            (channel, chat_id, thread_id)).fetchone()

    def upsert_link(self, channel: str, chat_id: int, thread_id: int = 0,
                    project: str | None = None, session_id: str | None = None,
                    title: str | None = None):
        existing = self.get_link(channel, chat_id, thread_id)
        if existing is None:
            self.db.execute(
                "INSERT INTO links(channel, chat_id, thread_id, project, session_id, "
                "title, created_at) VALUES(?,?,?,?,?,?,?)",
                (channel, chat_id, thread_id, project, session_id or str(uuid.uuid4()),
                 title, now_iso()))
            return self.get_link(channel, chat_id, thread_id)
        return existing

    def set_link_project(self, channel: str, chat_id: int, thread_id: int,
                         project: str, session_id: str | None = None) -> None:
        self.upsert_link(channel, chat_id, thread_id, project=project, session_id=session_id)
        if session_id:
            self.db.execute(
                "UPDATE links SET project=?, session_id=? WHERE channel=? AND chat_id=? "
                "AND thread_id=?", (project, session_id, channel, chat_id, thread_id))
        else:
            self.db.execute(
                "UPDATE links SET project=? WHERE channel=? AND chat_id=? AND thread_id=?",
                (project, channel, chat_id, thread_id))

    def mark_session_started(self, link_id: int) -> None:
        """Сессия нейросети заведена: со следующего раза продолжаем разговор."""
        self.db.execute("UPDATE links SET session_started=1 WHERE id=?", (link_id,))

    def reset_session(self, link_id: int, session_id: str) -> None:
        """Новый разговор в той же папке: ключ другой, продолжать нечего."""
        self.db.execute("UPDATE links SET session_id=?, session_started=0 WHERE id=?",
                        (session_id, link_id))

    def get_link_by_id(self, link_id: int):
        return self.db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()

    def touch_link(self, link_id: int, state: str = "idle") -> None:
        self.db.execute("UPDATE links SET last_job_at=?, state=? WHERE id=?",
                        (now_iso(), state, link_id))

    def list_links(self):
        return self.db.execute("SELECT * FROM links ORDER BY id").fetchall()

    def newest_link_of(self, channel: str):
        """Самый свежий чат канала: туда мост говорит то, что говорит сам.

        Сводка и «отдай» уходят во все настроенные мессенджеры, но в каждом —
        в один чат, а не во все, где человек когда-либо здоровался.
        """
        return self.db.execute(
            "SELECT * FROM links WHERE channel=? "
            "ORDER BY COALESCE(last_job_at, created_at) DESC, id DESC LIMIT 1",
            (channel,)).fetchone()

    # --- белый список -------------------------------------------------------

    def sync_allowlist(self, channel: str, user_ids: list[int]) -> None:
        """Список из настроек — источник правды: что не в файле, того нет и в базе."""
        self.db.execute("DELETE FROM allowlist WHERE channel=?", (channel,))
        for user_id in user_ids:
            self.db.execute(
                "INSERT OR IGNORE INTO allowlist(channel, user_id, added_at) VALUES(?,?,?)",
                (channel, int(user_id), now_iso()))

    def allow(self, channel: str, user_id: int, note: str | None = None) -> bool:
        """Пускает человека. True — значит, раньше его в списке не было.

        Список живёт в базе, и демон читает его на каждом сообщении: пустили
        человека — он заговорил с ботом сразу, без перезапуска моста.
        """
        if self.is_allowed(channel, user_id):
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO allowlist(channel, user_id, note, added_at) VALUES(?,?,?,?)",
            (channel, int(user_id), note, now_iso()))
        return True

    def deny(self, channel: str, user_id: int) -> bool:
        """Убирает человека из своих. True — значит, он там был."""
        if not self.is_allowed(channel, user_id):
            return False
        self.db.execute("DELETE FROM allowlist WHERE channel=? AND user_id=?",
                        (channel, int(user_id)))
        return True

    def list_allowed(self, channel: str | None = None):
        if channel is None:
            return self.db.execute(
                "SELECT * FROM allowlist ORDER BY channel, user_id").fetchall()
        return self.db.execute(
            "SELECT * FROM allowlist WHERE channel=? ORDER BY user_id", (channel,)).fetchall()

    def is_allowed(self, channel: str, user_id: int) -> bool:
        row = self.db.execute("SELECT 1 FROM allowlist WHERE channel=? AND user_id=?",
                              (channel, int(user_id))).fetchone()
        return row is not None

    # --- журнал -------------------------------------------------------------

    def note(self, kind: str, channel: str | None = None, chat_id: int | None = None,
             user_id: int | None = None, text: str = "", name: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO journal(at, kind, channel, chat_id, user_id, name, text) "
            "VALUES(?,?,?,?,?,?,?)",
            (now_iso(), kind, channel, chat_id, user_id, name, text))

    def note_stranger(self, channel: str, chat_id: int, user_id: int, text: str,
                      name: str = "") -> None:
        """Чужой стук: молчим в чат, но пишем сюда — иначе молчание неотличимо от поломки.

        Имя пишем рядом с номером нарочно: человек своего номера в мессенджере
        не знает и не должен, а себя в списке стучавшихся он узнаёт по имени.
        """
        head = (text or "").strip().replace("\n", " ")[:KNOCK_TEXT_LIMIT]
        self.note("stranger", channel=channel, chat_id=chat_id, user_id=user_id,
                  text=head, name=(name or "").strip()[:KNOCK_NAME_LIMIT] or None)

    def last_knocks(self) -> dict:
        """Последний постучавшийся в каждом канале: «пусти меня» — это про него."""
        rows = self.db.execute(
            "SELECT channel, user_id FROM journal WHERE kind='stranger' "
            "ORDER BY id DESC").fetchall()
        out: dict = {}
        for row in rows:
            out.setdefault(row["channel"], int(row["user_id"]))
        return out

    def recent_strangers(self, limit: int = 20):
        return self.db.execute(
            "SELECT * FROM journal WHERE kind='stranger' ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()

    def recent_journal(self, limit: int = 50):
        return self.db.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()

    # --- работы -------------------------------------------------------------

    def start_job(self, link_id: int | None, channel: str, chat_id: int,
                  session_id: str, prompt: str, job_dir: str, job_id: str | None = None) -> str:
        job_id = job_id or uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO jobs(id, link_id, channel, chat_id, session_id, prompt_head, dir, "
            "state, started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, link_id, channel, chat_id, session_id, (prompt or "")[:200],
             job_dir, JOB_RUNNING, now_iso()))
        return job_id

    def finish_job(self, job_id: str, state: str, exit_code: int | None = None,
                   duration_sec: float | None = None, result_head: str | None = None,
                   cost_usd: float | None = None) -> None:
        head = (result_head or "").strip().replace("\n", " ")[:RESULT_HEAD_LIMIT] or None
        self.db.execute(
            "UPDATE jobs SET state=?, exit_code=?, finished_at=?, duration_sec=?, "
            "result_head=COALESCE(?, result_head), cost_usd=COALESCE(?, cost_usd) WHERE id=?",
            (state, exit_code, now_iso(), duration_sec, head, cost_usd, job_id))

    def set_job_schedule(self, job_id: str, schedule_id: int) -> None:
        """Работу завёл будильник: по этой метке собирается «что запускалось ночью»."""
        self.db.execute("UPDATE jobs SET schedule_id=? WHERE id=?", (int(schedule_id), job_id))

    def set_job_pid(self, job_id: str, pid: int | None) -> None:
        """Номер процесса нейросети. Без него после падения моста её не найти:
        работа в базе значится живой, а гасить нечего."""
        self.db.execute("UPDATE jobs SET pid=? WHERE id=?",
                        (int(pid) if pid else None, job_id))

    def running_jobs(self):
        return self.db.execute(
            "SELECT * FROM jobs WHERE state=? ORDER BY started_at", (JOB_RUNNING,)).fetchall()

    def set_job_dir(self, job_id: str, job_dir: str) -> None:
        """Папку журнала знает только исполнитель — записываем, когда он её завёл."""
        self.db.execute("UPDATE jobs SET dir=? WHERE id=?", (str(job_dir), job_id))

    def mark_running_interrupted(self) -> list:
        """После перезагрузки: всё, что значилось работающим, работать уже не может.

        Возвращает прерванные работы — по ним мост говорит в чат честное
        «меня прервали», а не молчит. Второй раз те же работы не вернутся.
        """
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE state=? ORDER BY started_at", (JOB_RUNNING,)).fetchall()
        if rows:
            self.db.execute(
                "UPDATE jobs SET state=?, finished_at=? WHERE state=?",
                (JOB_INTERRUPTED, now_iso(), JOB_RUNNING))
            self.db.execute("UPDATE links SET state='idle' WHERE state<>'idle'")
        return list(rows)

    def jobs_of_link(self, link_id: int, limit: int = 5):
        return self.db.execute(
            "SELECT * FROM jobs WHERE link_id=? ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (link_id, limit)).fetchall()

    def last_finished_job(self, link_id: int):
        marks = ",".join("?" * len(JOB_FINISHED_STATES))
        return self.db.execute(
            f"SELECT * FROM jobs WHERE link_id=? AND state IN ({marks}) "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (link_id, *JOB_FINISHED_STATES)).fetchone()

    def get_job(self, job_id: str):
        return self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def list_jobs(self, limit: int = 20):
        return self.db.execute("SELECT * FROM jobs ORDER BY started_at DESC, rowid DESC LIMIT ?",
                               (limit,)).fetchall()

    # --- расписание ---------------------------------------------------------
    # Номер задачи, который человек видит в чате, — это id строки. Номера не
    # съезжают после удаления соседней задачи: «убери задачу 2» через неделю
    # уберёт ту же самую задачу, а не другую.

    def add_schedule(self, link_id: int, spec: str, prompt: str,
                     project: str | None = None, next_run_at: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO schedule(link_id, spec, prompt, created_at, project, next_run_at) "
            "VALUES(?,?,?,?,?,?)",
            (link_id, spec, prompt, now_iso(), project, next_run_at))
        return cur.lastrowid

    def list_schedule(self, only_enabled: bool = False):
        sql = "SELECT * FROM schedule"
        if only_enabled:
            sql += " WHERE enabled=1"
        return self.db.execute(sql + " ORDER BY id").fetchall()

    def get_schedule(self, schedule_id: int):
        return self.db.execute("SELECT * FROM schedule WHERE id=?",
                               (int(schedule_id),)).fetchone()

    def schedule_of_link(self, link_id: int):
        return self.db.execute("SELECT * FROM schedule WHERE link_id=? ORDER BY id",
                               (int(link_id),)).fetchall()

    def set_schedule_spec(self, schedule_id: int, spec: str,
                          next_run_at: str | None = None) -> None:
        self.db.execute("UPDATE schedule SET spec=?, next_run_at=? WHERE id=?",
                        (spec, next_run_at, int(schedule_id)))

    def set_schedule_next(self, schedule_id: int, next_run_at: str | None) -> None:
        self.db.execute("UPDATE schedule SET next_run_at=? WHERE id=?",
                        (next_run_at, int(schedule_id)))

    def enable_schedule(self, schedule_id: int, enabled: bool = True) -> None:
        self.db.execute("UPDATE schedule SET enabled=? WHERE id=?",
                        (1 if enabled else 0, int(schedule_id)))

    def remove_schedule(self, schedule_id: int) -> None:
        self.db.execute("DELETE FROM schedule WHERE id=?", (int(schedule_id),))

    def mark_schedule_run(self, schedule_id: int, status: str, at: str | None = None,
                          next_run_at: str | None = None) -> None:
        """Чем кончился прогон и когда следующий. Пустой next_run_at не затираем."""
        self.db.execute(
            "UPDATE schedule SET last_status=?, last_run_at=?, "
            "next_run_at=COALESCE(?, next_run_at) WHERE id=?",
            (status, at or now_iso(), next_run_at, int(schedule_id)))

    # --- срез за сутки: из него складывается сводка --------------------------

    def jobs_since(self, since: str, only_scheduled: bool = False):
        sql = "SELECT * FROM jobs WHERE started_at>=?"
        if only_scheduled:
            sql += " AND schedule_id IS NOT NULL"
        return self.db.execute(sql + " ORDER BY started_at, rowid", (since,)).fetchall()

    def spent_since(self, since: str) -> tuple[float, int]:
        """Сколько потрачено за срок и по скольким работам цена вообще известна.

        Считаем только то, что нейросеть назвала сама: придумывать цену там,
        где её не сказали, — врать человеку про его же деньги.
        """
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS spent, COUNT(cost_usd) AS known "
            "FROM jobs WHERE started_at>=?", (since,)).fetchone()
        return (float(row["spent"] or 0.0), int(row["known"] or 0))

    def strangers_since(self, since: str):
        return self.db.execute(
            "SELECT * FROM journal WHERE kind='stranger' AND at>=? ORDER BY id",
            (since,)).fetchall()

    def journal_since(self, kind: str, since: str):
        return self.db.execute(
            "SELECT * FROM journal WHERE kind=? AND at>=? ORDER BY id",
            (kind, since)).fetchall()
