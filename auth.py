"""Microsoft identity platform: device code (primary), PKCE redirect (fallback), refresh."""
from __future__ import annotations

import httpx

from config import AppConfig


class AuthError(Exception):
    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}" if description else code)
        self.code, self.description = code, description


class Auth:
    def __init__(self, http: httpx.Client, cfg: AppConfig):
        self.http, self.cfg = http, cfg

    def _token(self, form: dict) -> dict:
        form = {"client_id": self.cfg.client_id, **form}
        if self.cfg.client_secret and form.get("grant_type") in ("authorization_code", "refresh_token"):
            form["client_secret"] = self.cfg.client_secret
        r = self.http.post(f"{self.cfg.authority}/oauth2/v2.0/token", data=form, timeout=30)
        body = r.json()
        if r.status_code != 200:
            raise AuthError(body.get("error", "error"), body.get("error_description", ""))
        return body

    def refresh(self, refresh_token: str) -> dict:
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": AppConfig.SCOPES})
