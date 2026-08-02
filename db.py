import sqlite3
import os
import json

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler.db")

# Fixed length, in minutes, for every group session (see scheduler.py).
GROUP_SESSION_LENGTH = 20
GROUP_SIZE_MAX = 3

GRADE_OPTIONS = ["Preschool", "Kindergarten"] + [str(n) for n in range(1, 13)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    school TEXT NOT NULL,
    grade TEXT NOT NULL DEFAULT '',
    session_length INTEGER NOT NULL DEFAULT 30,
    minutes_seen INTEGER NOT NULL DEFAULT 0,
    availability TEXT NOT NULL DEFAULT '[]',
    groupable INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS work_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_of_week INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    school TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    day_of_week INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    school TEXT NOT NULL,
    is_group INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entry_students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    minutes_credited INTEGER,
    FOREIGN KEY (entry_id) REFERENCES schedule_entries(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(id),
    UNIQUE(entry_id, student_id)
);

CREATE TABLE IF NOT EXISTS event_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    day_of_week INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    label TEXT NOT NULL
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn):
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='students'").fetchone():
        if "groupable" not in _columns(conn, "students"):
            conn.execute("ALTER TABLE students ADD COLUMN groupable INTEGER NOT NULL DEFAULT 0")
        if "grade" not in _columns(conn, "students"):
            conn.execute("ALTER TABLE students ADD COLUMN grade TEXT NOT NULL DEFAULT ''")

    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schedule_entries'").fetchone():
        cols = _columns(conn, "schedule_entries")
        if "student_id" in cols:
            conn.execute("ALTER TABLE schedule_entries RENAME TO schedule_entries_old")
            conn.executescript("""
                CREATE TABLE schedule_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start TEXT NOT NULL,
                    day_of_week INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    school TEXT NOT NULL,
                    is_group INTEGER NOT NULL DEFAULT 0,
                    locked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS entry_students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    seen INTEGER NOT NULL DEFAULT 0,
                    minutes_credited INTEGER,
                    FOREIGN KEY (entry_id) REFERENCES schedule_entries(id) ON DELETE CASCADE,
                    FOREIGN KEY (student_id) REFERENCES students(id),
                    UNIQUE(entry_id, student_id)
                );
            """)
            old_rows = conn.execute("SELECT * FROM schedule_entries_old").fetchall()
            for r in old_rows:
                conn.execute(
                    """INSERT INTO schedule_entries
                       (id, week_start, day_of_week, start_time, end_time, school, is_group, locked)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                    (r["id"], r["week_start"], r["day_of_week"], r["start_time"],
                     r["end_time"], r["school"], r["locked"]),
                )
                if r["student_id"] is not None:
                    conn.execute(
                        """INSERT INTO entry_students (entry_id, student_id, seen, minutes_credited)
                           VALUES (?, ?, ?, ?)""",
                        (r["id"], r["student_id"], r["seen"], r["minutes_credited"]),
                    )
            conn.execute("DROP TABLE schedule_entries_old")

    conn.commit()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    conn.close()


def row_to_student(row):
    d = dict(row)
    d["availability"] = json.loads(d["availability"] or "[]")
    return d
