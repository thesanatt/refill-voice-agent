import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
DB_PATH = PACKAGE_DIR.parent / "refill.db"


def utc_now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@contextmanager
def transaction():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with transaction() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript((PACKAGE_DIR / "schema.sql").read_text(encoding="utf-8"))
        conn.executescript((PACKAGE_DIR / "seed.sql").read_text(encoding="utf-8"))


def load_session(conn, session_id):
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()


def update_session(conn, session_id, **fields):
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", (*fields.values(), session_id))


def add_message(conn, session_id, channel, role, text):
    conn.execute(
        "INSERT INTO messages (session_id, channel, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, channel, role, text, utc_now()),
    )
