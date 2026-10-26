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

DROP_MAX_ATTEMPTS = 3      # drop mode: deliveries of one email in all (the first + lease retries)

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (key_id TEXT PRIMARY KEY, account TEXT, tenant TEXT,
    refresh_token TEXT, connected_at REAL);
CREATE TABLE IF NOT EXISTS connects (connect_id TEXT PRIMARY KEY, data TEXT, expires_at REAL,
    status TEXT DEFAULT 'pending', connection_key TEXT);
CREATE TABLE IF NOT EXISTS cursors (key_id TEXT, scope TEXT, delta_link TEXT, since TEXT,
    PRIMARY KEY (key_id, scope));
CREATE TABLE IF NOT EXISTS queue (key_id TEXT, scope TEXT, message_id TEXT, received_at TEXT,
    delivered INTEGER DEFAULT 0, delivered_at REAL, PRIMARY KEY (key_id, scope, message_id));
CREATE TABLE IF NOT EXISTS subscriptions (key_id TEXT, resource TEXT, connection_key TEXT, sub_id TEXT,
    client_state TEXT, expires_at REAL, last_wait_at REAL, error TEXT, PRIMARY KEY (key_id, resource));
CREATE TABLE IF NOT EXISTS hub_positions (partition TEXT PRIMARY KEY, sequence INTEGER);
"""


class Store:
    def __init__(self, path: str, secret_key: str, clock=time.time):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        have = {r[1] for r in self._db.execute("PRAGMA table_info(queue)")}
        conn_cols = {r[1] for r in self._db.execute("PRAGMA table_info(connections)")}
        if "kind" not in conn_cols:
            self._db.execute("ALTER TABLE connections ADD COLUMN kind TEXT DEFAULT 'user'")
        if "first_round_at" not in conn_cols:      # organization: when a poll round first reached every mailbox
            self._db.execute("ALTER TABLE connections ADD COLUMN first_round_at REAL")
        for col in ("attempts", "in_folder", "baseline"):
            if col not in have:
                self._db.execute(f"ALTER TABLE queue ADD COLUMN {col} INTEGER DEFAULT 0")
        self._db.commit()
        self._fernet = Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)
        self._lock = threading.Lock()
        self._clock = clock

    @staticmethod
    def key_id(connection_key: str) -> str:
        return hashlib.sha256(connection_key.encode()).hexdigest()

    def _x(self, sql: str, args: tuple = ()) -> list[tuple]:
        """One statement; its rows are fetched under the lock (tool calls run on worker threads)."""
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    def _one(self, sql: str, args: tuple = ()) -> tuple | None:
        rows = self._x(sql, args)
        return rows[0] if rows else None

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
        self._x("INSERT OR REPLACE INTO connections (key_id, account, tenant, refresh_token, connected_at) VALUES (?, ?, ?, ?, ?)",
                (self.key_id(connection_key), account, tenant, self._enc(refresh_token), self._clock()))

    def save_org_connection(self, connection_key: str, group: str, tenant: str) -> None:
        """An organization connection: app-only, so no refresh token. `group` is the group's display name."""
        self._x("INSERT OR REPLACE INTO connections (key_id, account, tenant, refresh_token, connected_at, kind) "
                "VALUES (?, ?, ?, NULL, ?, 'organization')", (self.key_id(connection_key), group, tenant, self._clock()))

    def connection(self, connection_key: str) -> dict | None:
        row = self._one("SELECT account, tenant, refresh_token, connected_at, kind FROM connections WHERE key_id = ?",
                        (self.key_id(connection_key),))
        if not row:
            return None
        return {"account": row[0], "tenant": row[1], "refresh_token": self._dec(row[2]) if row[2] else "",
                "connected_at": row[3], "kind": row[4] or "user"}

    def first_round_done(self, connection_key: str) -> bool:
        """Has an organization poll round reached every member mailbox yet? Until then, new scopes are baselines."""
        row = self._one("SELECT first_round_at FROM connections WHERE key_id = ?", (self.key_id(connection_key),))
        return bool(row and row[0] is not None)

    def mark_first_round(self, connection_key: str) -> None:
        self._x("UPDATE connections SET first_round_at = ? WHERE key_id = ? AND first_round_at IS NULL",
                (self._clock(), self.key_id(connection_key)))

    def update_refresh_token(self, connection_key: str, refresh_token: str) -> None:
        self._x("UPDATE connections SET refresh_token = ? WHERE key_id = ?",
                (self._enc(refresh_token), self.key_id(connection_key)))

    # pending connects --------------------------------------------------------
    def create_connect(self, connect_id: str, data: dict, expires_at: float) -> None:
        self._x("INSERT INTO connects (connect_id, data, expires_at) VALUES (?, ?, ?)",
                (connect_id, json.dumps(data), expires_at))

    def connect(self, connect_id: str) -> dict | None:
        row = self._one("SELECT data, expires_at, status FROM connects WHERE connect_id = ?", (connect_id,))
        return {"data": json.loads(row[0]), "expires_at": row[1], "status": row[2]} if row else None

    def update_connect(self, connect_id: str, data: dict) -> None:
        self._x("UPDATE connects SET data = ? WHERE connect_id = ?", (json.dumps(data), connect_id))

    def finish_connect(self, connect_id: str, status: str, connection_key: str | None = None) -> bool:
        """End a pending connect. False when it had already ended (a concurrent request won): a late
        failure or expiry never overwrites a connected row, nor drops its one-time key."""
        with self._tx() as db:
            return db.execute("UPDATE connects SET status = ?, connection_key = ? WHERE connect_id = ? AND status = 'pending'",
                              (status, self._enc(connection_key) if connection_key else None, connect_id)).rowcount == 1

    def reopen_connect(self, connect_id: str, data: dict, expires_at: float) -> bool:
        """An ended (not connected) connect becomes pending again, for the browser sign-in fallback."""
        with self._tx() as db:
            return db.execute("UPDATE connects SET status = 'pending', data = ?, expires_at = ? "
                              "WHERE connect_id = ? AND status != 'connected'",
                              (json.dumps(data), expires_at, connect_id)).rowcount == 1

    def take_connection_key(self, connect_id: str) -> str | None:
        """Atomic: the one-time key is handed to exactly one caller."""
        with self._tx() as db:
            row = db.execute("SELECT connection_key FROM connects WHERE connect_id = ?", (connect_id,)).fetchone()
            if not row or not row[0]:
                return None
            db.execute("UPDATE connects SET connection_key = NULL WHERE connect_id = ?", (connect_id,))
        return self._dec(row[0])

    def connect_by_state(self, state: str) -> str | None:
        for cid, data in self._x("SELECT connect_id, data FROM connects WHERE status = 'pending'"):
            if json.loads(data).get("state") == state:
                return cid
        return None

    # cursors + queue ---------------------------------------------------------
    def cursor(self, connection_key: str, scope: str) -> dict | None:
        row = self._one("SELECT delta_link, since FROM cursors WHERE key_id = ? AND scope = ?",
                        (self.key_id(connection_key), scope))
        return {"delta_link": row[0], "since": row[1]} if row else None

    def has_cursor(self, connection_key: str) -> bool:
        """Has this connection polled any folder + filter set yet?"""
        return self._one("SELECT 1 FROM cursors WHERE key_id = ? LIMIT 1", (self.key_id(connection_key),)) is not None

    def save_cursor(self, connection_key: str, scope: str, delta_link: str | None, since: str) -> None:
        """delta_link None: `since` is fixed but no delta round has completed yet."""
        self._x("INSERT OR REPLACE INTO cursors VALUES (?, ?, ?, ?)",
                (self.key_id(connection_key), scope, delta_link, since))

    def enqueue(self, connection_key: str, scope: str, items: list[tuple[str, str]]) -> None:
        kid = self.key_id(connection_key)
        with self._tx() as db:
            db.executemany("INSERT OR IGNORE INTO queue (key_id, scope, message_id, received_at) VALUES (?, ?, ?, ?)",
                           [(kid, scope, m, r) for m, r in items])

    def newest_received(self, connection_key: str, scope: str) -> str | None:
        """receivedDateTime of the newest mail ever queued for this scope, delivered or not."""
        row = self._one("SELECT MAX(received_at) FROM queue WHERE key_id = ? AND scope = ?",
                        (self.key_id(connection_key), scope))
        return row[0] if row else None

    def enqueue_drop(self, connection_key: str, scope: str, items: list[tuple[str, str]], baseline: bool) -> None:
        """Drop-folder rows: in_folder until forgotten. Baseline rows (mail already there when the watch
        started) are born delivered, so they are never claimed."""
        kid = self.key_id(connection_key)
        with self._tx() as db:
            db.executemany("INSERT OR IGNORE INTO queue (key_id, scope, message_id, received_at, delivered, in_folder, baseline) "
                           "VALUES (?, ?, ?, ?, ?, 1, ?)",
                           [(kid, scope, m, r, int(baseline), int(baseline)) for m, r in items])

    def arrived_again(self, key_id: str, folder_id: str, message_id: str, lease_s: float,
                      max_attempts: int = DROP_MAX_ATTEMPTS) -> int:
        """An email already queued by a drop watch of this folder came into it again (Graph's "created"): its
        rows start over -- deliverable now, attempt 1. Only rows that are not mid-run: undelivered, lease run out,
        attempts exhausted, or baseline. Graph also sends "created" (at least once, maybe after the delta) for a
        first arrival, and that must not re-deliver an email just claimed. Drop rows are the only ones with
        in_folder = 1 (enqueue_drop); "new"-mode rows are never touched. No row: nothing is added (the next delta
        round queues new mail). Returns the number of rows reset."""
        prefix = folder_id + "|"                          # scope = "{folder_id}|{signature}"
        with self._tx() as db:
            return db.execute("UPDATE queue SET delivered = 0, delivered_at = NULL, attempts = 0, baseline = 0 "
                              "WHERE key_id = ? AND message_id = ? AND in_folder = 1 AND substr(scope, 1, ?) = ? "
                              "AND (delivered = 0 OR delivered_at <= ? OR attempts >= ? OR baseline = 1)",
                              (key_id, message_id, len(prefix), prefix, self._clock() - lease_s, max_attempts)).rowcount

    def queued_except(self, connection_key: str, scope: str, keep: set[str]) -> list[str]:
        """Ids of this scope's rows that are not in `keep`."""
        rows = self._x("SELECT message_id FROM queue WHERE key_id = ? AND scope = ?", (self.key_id(connection_key), scope))
        return [m for (m,) in rows if m not in keep]

    def forget(self, connection_key: str, scope: str, message_ids: list[str]) -> None:
        kid = self.key_id(connection_key)
        with self._tx() as db:
            db.executemany("DELETE FROM queue WHERE key_id = ? AND scope = ? AND message_id = ?",
                           [(kid, scope, m) for m in message_ids])

    def attempts(self, connection_key: str, scope: str, message_id: str) -> int:
        row = self._one("SELECT attempts FROM queue WHERE key_id = ? AND scope = ? AND message_id = ?",
                        (self.key_id(connection_key), scope, message_id))
        return row[0] if row else 0

    def _drop_due(self, lease_s: float, max_attempts: int) -> tuple[str, tuple]:
        return ("in_folder = 1 AND baseline = 0 AND attempts < ? AND (delivered = 0 OR delivered_at <= ?)",
                (max_attempts, self._clock() - lease_s))

    def claim_next(self, connection_key: str, scope: str, lease_s: float | None = None,
                   max_attempts: int = DROP_MAX_ATTEMPTS) -> str | None:
        """Oldest undelivered message, marked delivered in the same transaction. Without lease_s delivery
        is at-most-once by design: a message claimed but lost by the caller is not offered again.
        With lease_s (drop mode) a claimed message that is still in the folder is offered again once the
        lease has run out, up to max_attempts times."""
        kid = self.key_id(connection_key)
        with self._tx() as db:
            if lease_s is None:
                cond, args = "delivered = 0", ()
            else:
                cond, args = self._drop_due(lease_s, max_attempts)
            row = db.execute(f"SELECT message_id FROM queue WHERE key_id = ? AND scope = ? AND {cond} "
                             "ORDER BY received_at, message_id LIMIT 1", (kid, scope, *args)).fetchone()
            if not row:
                return None
            db.execute("UPDATE queue SET delivered = 1, delivered_at = ?, attempts = attempts + 1 "
                       "WHERE key_id = ? AND scope = ? AND message_id = ?",
                       (self._clock(), kid, scope, row[0]))
            return row[0]

    def claim_next_of(self, connection_key: str, scopes: list[str], lease_s: float | None = None,
                      max_attempts: int = DROP_MAX_ATTEMPTS) -> tuple[str, str] | None:
        """claim_next across several scopes (an organization's mailboxes): (scope, message id) of the oldest."""
        if not scopes:
            return None
        kid = self.key_id(connection_key)
        cond, args = ("delivered = 0", ()) if lease_s is None else self._drop_due(lease_s, max_attempts)
        with self._tx() as db:
            row = db.execute(f"SELECT scope, message_id FROM queue WHERE key_id = ? AND scope IN "
                             f"({', '.join('?' * len(scopes))}) AND {cond} ORDER BY received_at, message_id LIMIT 1",
                             (kid, *scopes, *args)).fetchone()
            if not row:
                return None
            db.execute("UPDATE queue SET delivered = 1, delivered_at = ?, attempts = attempts + 1 "
                       "WHERE key_id = ? AND scope = ? AND message_id = ?", (self._clock(), kid, *row))
            return row[0], row[1]

    def purge_scope_prefix(self, connection_key: str, prefix: str) -> int:
        """Forget the queue rows and cursors of every scope starting with `prefix` (a mailbox that left the group).
        Delivered rows go too: if the mailbox comes back, its drop folder is read afresh, with no baseline.
        Returns the number of queue rows removed."""
        kid, n = self.key_id(connection_key), len(prefix)
        with self._tx() as db:
            db.execute("DELETE FROM cursors WHERE key_id = ? AND substr(scope, 1, ?) = ?", (kid, n, prefix))
            return db.execute("DELETE FROM queue WHERE key_id = ? AND substr(scope, 1, ?) = ?", (kid, n, prefix)).rowcount

    def release(self, connection_key: str, scope: str, message_id: str) -> None:
        """Undo a claim whose message the server itself failed to fetch: it was never handed to anyone,
        so the attempt does not count either."""
        self._x("UPDATE queue SET delivered = 0, delivered_at = NULL, attempts = MAX(attempts - 1, 0) "
                "WHERE key_id = ? AND scope = ? AND message_id = ?",
                (self.key_id(connection_key), scope, message_id))

    def has_undelivered(self, connection_key: str, scope: str, lease_s: float | None = None,
                        max_attempts: int = DROP_MAX_ATTEMPTS) -> bool:
        if lease_s is None:
            cond, args = "delivered = 0", ()
        else:
            cond, args = self._drop_due(lease_s, max_attempts)
        return self._one(f"SELECT 1 FROM queue WHERE key_id = ? AND scope = ? AND {cond} LIMIT 1",
                         (self.key_id(connection_key), scope, *args)) is not None

    # change subscriptions (events) ---------------------------------------------
    # The connection key is kept (encrypted) because the keeper renews subscriptions with no request in hand.
    def _sub(self, row: tuple) -> dict:
        return {"key_id": row[0], "resource": row[1], "connection_key": self._dec(row[2]), "sub_id": row[3],
                "client_state": self._dec(row[4]) if row[4] else "", "expires_at": row[5], "last_wait_at": row[6],
                "error": row[7]}

    _SUB_COLS = "key_id, resource, connection_key, sub_id, client_state, expires_at, last_wait_at, error"

    def subscription(self, connection_key: str, resource: str) -> dict | None:
        row = self._one(f"SELECT {self._SUB_COLS} FROM subscriptions WHERE key_id = ? AND resource = ?",
                        (self.key_id(connection_key), resource))
        return self._sub(row) if row else None

    def subscription_by_id(self, sub_id: str) -> dict | None:
        row = self._one(f"SELECT {self._SUB_COLS} FROM subscriptions WHERE sub_id = ?", (sub_id,))
        return self._sub(row) if row else None

    def subscriptions(self) -> list[dict]:
        return [self._sub(r) for r in self._x(f"SELECT {self._SUB_COLS} FROM subscriptions")]

    def save_subscription(self, connection_key: str, resource: str, sub_id: str | None, client_state: str,
                          expires_at: float, error: str | None = None) -> None:
        """Create or replace; last_wait_at is kept (a renewal is not a wait)."""
        kid = self.key_id(connection_key)
        with self._tx() as db:
            row = db.execute("SELECT last_wait_at FROM subscriptions WHERE key_id = ? AND resource = ?", (kid, resource)).fetchone()
            db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (kid, resource, self._enc(connection_key), sub_id, self._enc(client_state) if client_state else "",
                        expires_at, row[0] if row else self._clock(), error))

    def touch_subscription(self, connection_key: str, resource: str) -> None:
        self._x("UPDATE subscriptions SET last_wait_at = ? WHERE key_id = ? AND resource = ?",
                (self._clock(), self.key_id(connection_key), resource))

    def delete_subscription(self, connection_key: str, resource: str) -> None:
        self._x("DELETE FROM subscriptions WHERE key_id = ? AND resource = ?", (self.key_id(connection_key), resource))

    def hub_positions(self) -> dict[str, int]:
        return {p: s for p, s in self._x("SELECT partition, sequence FROM hub_positions")}

    def save_hub_position(self, partition: str, sequence: int) -> None:
        self._x("INSERT OR REPLACE INTO hub_positions VALUES (?, ?)", (partition, sequence))

    def prune(self, days: int = 30) -> None:
        self._x("DELETE FROM queue WHERE delivered = 1 AND in_folder = 0 AND delivered_at < ?", (self._clock() - days * 86400,))
