"""SQLite cache: the last good response per endpoint, fetch outcomes, looked-up
terminal distances, your terminal reads, SC Trade Tools reports, the wallet
history and small bits of app state (last wallet read)."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoint (
    key TEXT PRIMARY KEY,          -- resource plus query, e.g. orbits_distances?id_star_system=68
    body TEXT,                     -- JSON of the response's data
    fetched_at REAL,               -- unix time of the last good fetch
    last_attempt REAL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS terminal_distance (
    a INTEGER, b INTEGER,          -- terminal ids, a < b
    gm REAL,
    fetched_at REAL,
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS capture (
    terminal_id INTEGER, commodity_id INTEGER,
    side TEXT,                     -- "buy" or "sell"
    price REAL, scu INTEGER, status INTEGER, boxes TEXT,
    captured_at REAL,              -- read off the in-game terminal screen
    PRIMARY KEY (terminal_id, commodity_id, side)
);
CREATE TABLE IF NOT EXISTS sctrade_listing (
    location TEXT, commodity TEXT, -- as SC Trade Tools names them, lower case
    side TEXT,                     -- "buy" (the shop sells) or "sell" (the shop buys)
    price REAL, scu INTEGER, saturation REAL, boxes TEXT,
    observed_at REAL,              -- when the player saw it
    PRIMARY KEY (location, commodity, side, observed_at, price)
);
CREATE TABLE IF NOT EXISTS wallet_log (
    at REAL,                       -- when the balance was read
    value INTEGER,
    source TEXT                    -- "ocr", "terminal" or "manual"
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def save(self, key: str, data, at: float) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO endpoint (key, body, fetched_at, last_attempt, last_error) VALUES (?, ?, ?, ?, NULL) "
                "ON CONFLICT(key) DO UPDATE SET body=excluded.body, fetched_at=excluded.fetched_at, "
                "last_attempt=excluded.last_attempt, last_error=NULL",
                (key, json.dumps(data, separators=(",", ":")), at, at),
            )

    def mark_failure(self, key: str, error: str, at: float) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO endpoint (key, last_attempt, last_error) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET last_attempt=excluded.last_attempt, last_error=excluded.last_error",
                (key, at, error),
            )

    def load(self, key: str):
        """(data, fetched_at), or (None, None) when never fetched successfully."""
        with self.lock:
            row = self.db.execute("SELECT body, fetched_at FROM endpoint WHERE key=?", (key,)).fetchone()
        if not row or row[0] is None:
            return None, None
        return json.loads(row[0]), row[1]

    def meta(self, key: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT fetched_at, last_attempt, last_error FROM endpoint WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return {"fetched_at": None, "last_attempt": None, "last_error": None}
        return {"fetched_at": row[0], "last_attempt": row[1], "last_error": row[2]}

    def save_terminal_distance(self, a: int, b: int, gm: float, at: float) -> None:
        a, b = min(a, b), max(a, b)
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO terminal_distance (a, b, gm, fetched_at) VALUES (?, ?, ?, ?)", (a, b, gm, at)
            )

    def terminal_distances(self, newer_than: float = 0) -> dict[tuple[int, int], float]:
        with self.lock:
            rows = self.db.execute(
                "SELECT a, b, gm FROM terminal_distance WHERE fetched_at >= ?", (newer_than,)
            ).fetchall()
        return {(a, b): gm for a, b, gm in rows}

    CAPTURE_FIELDS = ("terminal_id", "commodity_id", "side", "price", "scu", "status", "boxes", "captured_at")

    def save_captures(self, rows: list[dict]) -> None:
        """Keep the latest capture per terminal, commodity and side."""
        with self.lock, self.db:
            self.db.executemany(
                f"INSERT OR REPLACE INTO capture ({', '.join(self.CAPTURE_FIELDS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [tuple(r.get(f) for f in self.CAPTURE_FIELDS) for r in rows],
            )

    def captures(self, newer_than: float = 0) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                f"SELECT {', '.join(self.CAPTURE_FIELDS)} FROM capture WHERE captured_at >= ?", (newer_than,)
            ).fetchall()
        return [dict(zip(self.CAPTURE_FIELDS, r)) for r in rows]

    LISTING_FIELDS = ("location", "commodity", "side", "price", "scu", "saturation", "boxes", "observed_at")

    def save_listings(self, rows: list[dict], older_than: float = 0) -> None:
        """Add SC Trade Tools reports and drop the ones too old to matter."""
        with self.lock, self.db:
            self.db.executemany(
                f"INSERT OR IGNORE INTO sctrade_listing ({', '.join(self.LISTING_FIELDS)}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [tuple(r.get(f) for f in self.LISTING_FIELDS) for r in rows],
            )
            self.db.execute("DELETE FROM sctrade_listing WHERE observed_at < ?", (older_than,))

    def listings(self, newer_than: float = 0) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                f"SELECT {', '.join(self.LISTING_FIELDS)} FROM sctrade_listing WHERE observed_at >= ?", (newer_than,)
            ).fetchall()
        return [dict(zip(self.LISTING_FIELDS, r)) for r in rows]

    def newest_listing(self) -> float:
        with self.lock:
            row = self.db.execute("SELECT MAX(observed_at) FROM sctrade_listing").fetchone()
        return row[0] or 0.0

    def log_wallet(self, at: float, value: int, source: str) -> bool:
        """Record a balance reading unless it repeats the last one. True when recorded."""
        with self.lock, self.db:
            last = self.db.execute("SELECT value FROM wallet_log ORDER BY at DESC, rowid DESC LIMIT 1").fetchone()
            if last and last[0] == value:
                return False
            self.db.execute("INSERT INTO wallet_log (at, value, source) VALUES (?, ?, ?)", (at, value, source))
        return True

    def wallet_log(self, newer_than: float = 0):
        from .wallet_log import Entry

        with self.lock:
            rows = self.db.execute(
                "SELECT at, value, source FROM wallet_log WHERE at >= ? ORDER BY at, rowid", (newer_than,)
            ).fetchall()
        return [Entry(*r) for r in rows]

    def get_state(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, json.dumps(value)))
