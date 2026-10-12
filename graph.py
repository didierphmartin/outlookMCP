"""Microsoft Graph over httpx: bearer tokens, one refresh on 401, one wait on 429."""
from __future__ import annotations

import threading
import time

import httpx

MAX_RETRY_AFTER_S = 10.0
PREFER_IMMUTABLE_IDS = 'IdType="ImmutableId"'   # a message keeps its id when it moves between folders


class GraphError(Exception):
    """A Graph failure the tools report as {"error": code}: reconnect_needed, throttled, not_found, network_error,
    forbidden (the sign-in lacks a permission), sync_state_lost (a delta link Graph no longer honours),
    graph_error (with the HTTP status)."""

    def __init__(self, code: str, message: str = "", status: int = 0):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message, self.status = code, message, status

    @property
    def transient(self) -> bool:
        """Worth retrying later: throttling, the network, a 5xx."""
        match self.code:
            case "throttled" | "network_error":
                return True
            case "graph_error":
                return self.status >= 500
            case _:
                return False

    @classmethod
    def from_auth(cls, e) -> "GraphError":
        """A failed token refresh: transient trouble reaching the sign-in service, or a dead grant (sign in again)."""
        match e.code:
            case "network_error" | "identity_error" | "temporarily_unavailable":
                return cls("network_error", str(e))
            case _:
                return cls("reconnect_needed", str(e))


class StaticToken:
    """A token that is already known (identity lookup right after sign-in)."""

    def __init__(self, token: str):
        self._token = token

    def access_token(self, force: bool = False) -> str:
        return self._token


class TokenSource:
    """Access tokens for one stored connection, refreshed from its (rotating) refresh token."""

    def __init__(self, auth, store, connection_key: str, clock=time.time):
        self.auth, self.store, self.key, self.clock = auth, store, connection_key, clock
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()                # tool calls run on worker threads: refresh once, not per call

    def access_token(self, force: bool = False) -> str:
        with self._lock:
            return self._access_token(force)

    def _access_token(self, force: bool) -> str:
        if self._token and not force and self.clock() < self._expires - 60:
            return self._token
        conn = self.store.connection(self.key)
        if conn is None:
            raise GraphError("reconnect_needed", "unknown or removed connection")
        from auth import AuthError
        try:
            tok = self.auth.refresh(conn["refresh_token"])
        except AuthError as e:
            raise GraphError.from_auth(e) from None
        if tok.get("refresh_token"):
            self.store.update_refresh_token(self.key, tok["refresh_token"])
        self._token, self._expires = tok["access_token"], self.clock() + float(tok.get("expires_in", 3600))
        return self._token


class GraphClient:
    def __init__(self, http: httpx.Client, base: str, tokens, sleep=time.sleep):
        self.http, self.base, self.tokens, self.sleep = http, base.rstrip("/"), tokens, sleep

    def _send(self, path_or_url: str, params: dict | None, method: str = "GET", body: dict | None = None) -> httpx.Response:
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        force = False
        for attempt in range(2):
            r = self.http.request(method, url, params=params, json=body, timeout=30,
                                  headers={"Authorization": "Bearer " + self.tokens.access_token(force=force),
                                           "Prefer": PREFER_IMMUTABLE_IDS})
            match r.status_code:
                case 200 | 201 | 202 | 204:
                    return r
                case 401 if attempt == 0:
                    force = True
                case 401:
                    raise GraphError("reconnect_needed", "Graph refused the refreshed token")
                case 429 if attempt == 0:
                    self.sleep(min(float(r.headers.get("Retry-After", "1")), MAX_RETRY_AFTER_S))
                case 429:
                    raise GraphError("throttled", "Microsoft Graph is throttling this mailbox; try again later")
                case 403:
                    raise GraphError("forbidden", "the sign-in lacks a permission (Mail.ReadWrite to move, Mail.Send "
                                                  "to forward): grant it in Entra, then connect the mailbox again")
                case 404:
                    raise GraphError("not_found", url)
                case 410 | 400 if self._sync_state_lost(r):
                    raise GraphError("sync_state_lost", f"{r.status_code}: {r.text[:200]}", status=r.status_code)
                case code:
                    raise GraphError("graph_error", f"{code}: {r.text[:200]}", status=code)
        raise GraphError("graph_error", "no response")

    @staticmethod
    def _sync_state_lost(r: httpx.Response) -> bool:
        """410 Gone, or a 400 naming the sync state (SyncStateNotFound, SyncStateInvalid, resync required)."""
        if r.status_code == 410:
            return True
        text = r.text.lower().replace(" ", "")
        return "syncstate" in text or "resync" in text

    def get(self, path_or_url: str, params: dict | None = None) -> dict:
        return self._send(path_or_url, params).json()

    def get_bytes(self, path_or_url: str) -> bytes:
        return self._send(path_or_url, None).content

    def post(self, path: str, body: dict) -> dict:
        """A write; {} when Graph answers without a body (202 Accepted for a forward)."""
        r = self._send(path, None, "POST", body)
        return r.json() if r.content else {}

    def patch(self, path: str, body: dict) -> dict:
        r = self._send(path, None, "PATCH", body)
        return r.json() if r.content else {}

    def delete(self, path: str) -> None:
        self._send(path, None, "DELETE")
