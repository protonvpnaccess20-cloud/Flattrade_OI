"""
Storage: Turso (libsql) when TURSO_URL + TURSO_TOKEN are set — survives
Render free-tier restarts, which wipe the local disk. Falls back to a
local SQLite file (fine on a Mac / for tests).

Tables:
  ft_session(day TEXT PRIMARY KEY, token TEXT, updated_at TEXT)
  oi_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, ts TEXT,
               symbol TEXT, payload TEXT)   -- payload = JSON of the view
  app_state(key TEXT PRIMARY KEY, value TEXT) -- last errors etc.
"""

import json
import os
import sqlite3

_conn = None


def _connect():
    global _conn
    if _conn is not None:
        return _conn
    url = os.environ.get("TURSO_URL", "").strip()
    token = os.environ.get("TURSO_TOKEN", "").strip()
    if url and token:
        import libsql_experimental as libsql

        _conn = libsql.connect(database=url, auth_token=token)
        backend = "turso"
    else:
        path = os.environ.get("OI_DB_PATH", os.path.join(os.path.dirname(__file__), "oi.db"))
        _conn = sqlite3.connect(path, check_same_thread=False)
        backend = "sqlite-local"
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS ft_session (day TEXT PRIMARY KEY, token TEXT, updated_at TEXT)"
    )
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS oi_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, ts TEXT, symbol TEXT, payload TEXT)"
    )
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)"
    )
    _conn.commit()
    print(f"[oidb] storage backend: {backend}")
    return _conn


def storage_backend() -> str:
    url = os.environ.get("TURSO_URL", "").strip()
    return "turso" if url else "sqlite-local"


def save_session(day: str, token: str, updated_at: str):
    c = _connect()
    c.execute("DELETE FROM ft_session WHERE day = ?", (day,))
    c.execute("INSERT INTO ft_session (day, token, updated_at) VALUES (?, ?, ?)", (day, token, updated_at))
    c.commit()


def get_session(day: str):
    c = _connect()
    row = c.execute("SELECT token, updated_at FROM ft_session WHERE day = ?", (day,)).fetchone()
    if not row:
        return None
    return {"token": row[0], "updated_at": row[1]}


def delete_session(day: str):
    c = _connect()
    c.execute("DELETE FROM ft_session WHERE day = ?", (day,))
    c.commit()


def add_snapshot(day: str, ts: str, symbol: str, payload: dict):
    c = _connect()
    c.execute(
        "INSERT INTO oi_snapshots (day, ts, symbol, payload) VALUES (?, ?, ?, ?)",
        (day, ts, symbol, json.dumps(payload)),
    )
    c.commit()


def last_snapshots(day: str, symbol: str, limit: int = 2):
    c = _connect()
    rows = c.execute(
        "SELECT ts, payload FROM oi_snapshots WHERE day = ? AND symbol = ? ORDER BY id DESC LIMIT ?",
        (day, symbol, limit),
    ).fetchall()
    return [{"ts": r[0], "payload": json.loads(r[1])} for r in rows]


def day_baseline(day: str, symbol: str):
    c = _connect()
    row = c.execute(
        "SELECT ts, payload FROM oi_snapshots WHERE day = ? AND symbol = ? ORDER BY id ASC LIMIT 1",
        (day, symbol),
    ).fetchone()
    if not row:
        return None
    return {"ts": row[0], "payload": json.loads(row[1])}


def set_state(key: str, value: str):
    c = _connect()
    c.execute("DELETE FROM app_state WHERE key = ?", (key,))
    c.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (key, value))
    c.commit()


def get_state(key: str):
    c = _connect()
    row = c.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None
