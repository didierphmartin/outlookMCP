"""Microsoft Graph over httpx: bearer tokens, one refresh on 401, one wait on 429."""
from __future__ import annotations

import time

import httpx

MAX_RETRY_AFTER_S = 10.0


class GraphError(Exception):
    """A Graph failure the tools report as {"error": code}: reconnect_needed, throttled, not_found, graph_error."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message = code, message


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

    def access_token(self, force: bool = False) -> str:
        if self._token and not force and self.clock() < self._expires - 60:
            return self._token
        conn = self.store.connection(self.key)
        if conn is None:
            raise GraphError("reconnect_needed", "unknown or removed connection")
        from auth import AuthError
        try:
            tok = self.auth.refresh(conn["refresh_token"])
        except AuthError as e:
            raise GraphError("reconnect_needed", str(e)) from None
        if tok.get("refresh_token"):
            self.store.update_refresh_token(self.key, tok["refresh_token"])
        self._token, self._expires = tok["access_token"], self.clock() + float(tok.get("expires_in", 3600))
        return self._token


class GraphClient:
    def __init__(self, http: httpx.Client, base: str, tokens, sleep=time.sleep):
        self.http, self.base, self.tokens, self.sleep = http, base.rstrip("/"), tokens, sleep

    def _send(self, path_or_url: str, params: dict | None) -> httpx.Response:
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        force = False
        for attempt in range(2):
            r = self.http.get(url, params=params,
                              headers={"Authorization": "Bearer " + self.tokens.access_token(force=force)}, timeout=30)
            match r.status_code:
                case 200:
                    return r
                case 401 if attempt == 0:
                    force = True
                case 401:
                    raise GraphError("reconnect_needed", "Graph refused the refreshed token")
                case 429 if attempt == 0:
                    self.sleep(min(float(r.headers.get("Retry-After", "1")), MAX_RETRY_AFTER_S))
                case 429:
                    raise GraphError("throttled", "Microsoft Graph is throttling this mailbox; try again later")
                case 404:
                    raise GraphError("not_found", url)
                case code:
                    raise GraphError("graph_error", f"{code}: {r.text[:200]}")
        raise GraphError("graph_error", "no response")

    def get(self, path_or_url: str, params: dict | None = None) -> dict:
        return self._send(path_or_url, params).json()

    def get_bytes(self, path_or_url: str) -> bytes:
        return self._send(path_or_url, None).content
