"""Everything the server remembers, in one SQLite file. Refresh tokens are Fernet-encrypted;
connection keys are stored only as SHA-256 (the app holds the key itself)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (key_id TEXT PRIMARY KEY, account TEXT, tenant TEXT,
    refresh_token TEXT, connected_at REAL);
CREATE TABLE IF NOT EXISTS connects (connect_id TEXT PRIMARY KEY, data TEXT, expires_at REAL,
    status TEXT DEFAULT 'pending', connection_key TEXT);
CREATE TABLE IF NOT EXISTS cursors (key_id TEXT, scope TEXT, delta_link TEXT, since TEXT,
    PRIMARY KEY (key_id, scope));
CREATE TABLE IF NOT EXISTS queue (key_id TEXT, scope TEXT, message_id TEXT, received_at TEXT,
    delivered INTEGER DEFAULT 0, delivered_at REAL, PRIMARY KEY (key_id, scope, message_id));
"""


class Store:
    def __init__(self, path: str, secret_key: str, clock=time.time):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        self._fernet = Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)
        self._lock = threading.Lock()
        self._clock = clock

    @staticmethod
    def key_id(connection_key: str) -> str:
        return hashlib.sha256(connection_key.encode()).hexdigest()

    def _x(self, sql: str, args: tuple = ()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    @contextmanager
    def _tx(self):
        """Several statements under one lock and one transaction."""
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _enc(self, s: str) -> str:
        return self._fernet.encrypt(s.encode()).decode()

    def _dec(self, s: str) -> str:
        return self._fernet.decrypt(s.encode()).decode()

    # connections -------------------------------------------------------------
    def save_connection(self, connection_key: str, account: str, tenant: str, refresh_token: str) -> None:
        self._x("INSERT OR REPLACE INTO connections VALUES (?, ?, ?, ?, ?)",
                (self.key_id(connection_key), account, tenant, self._enc(refresh_token), self._clock()))

    def connection(self, connection_key: str) -> dict | None:
        row = self._x("SELECT account, tenant, refresh_token, connected_at FROM connections WHERE key_id = ?",
                      (self.key_id(connection_key),)).fetchone()
        if not row:
            return None
        return {"account": row[0], "tenant": row[1], "refresh_token": self._dec(row[2]), "connected_at": row[3]}

    def update_refresh_token(self, connection_key: str, refresh_token: str) -> None:
        self._x("UPDATE connections SET refresh_token = ? WHERE key_id = ?",
                (self._enc(refresh_token), self.key_id(connection_key)))

    # pending connects --------------------------------------------------------
    def create_connect(self, connect_id: str, data: dict, expires_at: float) -> None:
        self._x("INSERT INTO connects (connect_id, data, expires_at) VALUES (?, ?, ?)",
                (connect_id, json.dumps(data), expires_at))

    def connect(self, connect_id: str) -> dict | None:
        row = self._x("SELECT data, expires_at, status FROM connects WHERE connect_id = ?", (connect_id,)).fetchone()
        return {"data": json.loads(row[0]), "expires_at": row[1], "status": row[2]} if row else None

    def update_connect(self, connect_id: str, data: dict) -> None:
        self._x("UPDATE connects SET data = ? WHERE connect_id = ?", (json.dumps(data), connect_id))

    def finish_connect(self, connect_id: str, status: str, connection_key: str | None = None) -> None:
        self._x("UPDATE connects SET status = ?, connection_key = ? WHERE connect_id = ?",
                (status, self._enc(connection_key) if connection_key else None, connect_id))

    def take_connection_key(self, connect_id: str) -> str | None:
        """Atomic: the one-time key is handed to exactly one caller."""
        with self._tx() as db:
            row = db.execute("SELECT connection_key FROM connects WHERE connect_id = ?", (connect_id,)).fetchone()
            if not row or not row[0]:
                return None
            db.execute("UPDATE connects SET connection_key = NULL WHERE connect_id = ?", (connect_id,))
        return self._dec(row[0])

    def connect_by_state(self, state: str) -> str | None:
        for cid, data in self._x("SELECT connect_id, data FROM connects WHERE status = 'pending'").fetchall():
            if json.loads(data).get("state") == state:
                return cid
        return None

    # cursors + queue ---------------------------------------------------------
    def cursor(self, connection_key: str, scope: str) -> dict | None:
        row = self._x("SELECT delta_link, since FROM cursors WHERE key_id = ? AND scope = ?",
                      (self.key_id(connection_key), scope)).fetchone()
        return {"delta_link": row[0], "since": row[1]} if row else None

    def save_cursor(self, connection_key: str, scope: str, delta_link: str, since: str) -> None:
        self._x("INSERT OR REPLACE INTO cursors VALUES (?, ?, ?, ?)",
                (self.key_id(connection_key), scope, delta_link, since))

    def enqueue(self, connection_key: str, scope: str, items: list[tuple[str, str]]) -> None:
        kid = self.key_id(connection_key)
        with self._tx() as db:
            db.executemany("INSERT OR IGNORE INTO queue (key_id, scope, message_id, received_at) VALUES (?, ?, ?, ?)",
                           [(kid, scope, m, r) for m, r in items])

    def claim_next(self, connection_key: str, scope: str) -> str | None:
        """Oldest undelivered message, marked delivered in the same transaction. Delivery is
        at-most-once by design: a message claimed but lost by the caller is not offered again."""
        kid = self.key_id(connection_key)
        with self._tx() as db:
            row = db.execute("SELECT message_id FROM queue WHERE key_id = ? AND scope = ? AND delivered = 0 "
                             "ORDER BY received_at, message_id LIMIT 1", (kid, scope)).fetchone()
            if not row:
                return None
            db.execute("UPDATE queue SET delivered = 1, delivered_at = ? WHERE key_id = ? AND scope = ? AND message_id = ?",
                       (self._clock(), kid, scope, row[0]))
            return row[0]

    def release(self, connection_key: str, scope: str, message_id: str) -> None:
        """Undo a claim whose message the server itself failed to fetch: it was never handed to anyone."""
        self._x("UPDATE queue SET delivered = 0, delivered_at = NULL WHERE key_id = ? AND scope = ? AND message_id = ?",
                (self.key_id(connection_key), scope, message_id))

    def prune(self, days: int = 30) -> None:
        self._x("DELETE FROM queue WHERE delivered = 1 AND delivered_at < ?", (self._clock() - days * 86400,))
