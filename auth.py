"""Microsoft identity platform: device code (primary), PKCE redirect (fallback), refresh."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx

from config import AppConfig

# The request header that carries a connection key; connect_status names it so the app stores the key there.
CREDENTIAL_HEADER = "X-Outlook-Account"


class AuthError(Exception):
    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}" if description else code)
        self.code, self.description = code, description


class Auth:
    def __init__(self, http: httpx.Client, cfg: AppConfig):
        self.http, self.cfg = http, cfg

    def _post(self, endpoint: str, form: dict) -> dict:
        """POST to the identity platform. Every failure (transport, non-JSON gateway page, OAuth error) is AuthError."""
        try:
            r = self.http.post(f"{self.cfg.authority}/oauth2/v2.0/{endpoint}", data=form, timeout=30)
        except httpx.HTTPError as e:
            raise AuthError("network_error", f"{type(e).__name__}: {e}"[:200]) from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise AuthError("identity_error", f"{r.status_code} from the sign-in service (not JSON)")
        if r.status_code != 200:
            raise AuthError(body.get("error", "error"), body.get("error_description", ""))
        return body

    def _token(self, form: dict) -> dict:
        form = {"client_id": self.cfg.client_id, **form}
        if self.cfg.client_secret:      # confidential registrations only; public clients must not send one (AADSTS700025)
            form["client_secret"] = self.cfg.client_secret
        return self._post("token", form)

    def refresh(self, refresh_token: str) -> dict:
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": AppConfig.SCOPES})

    # device code -------------------------------------------------------------------
    def device_start(self) -> dict:
        return self._post("devicecode", {"client_id": self.cfg.client_id, "scope": AppConfig.SCOPES})

    def device_poll(self, device_code: str) -> tuple[str, dict | None]:
        """(status, tokens) when connected; ("failed", {"message": reason}) on a terminal error; else (status, None)."""
        try:
            return "connected", self._token({"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                             "device_code": device_code})
        except AuthError as e:
            match e.code:
                case "authorization_pending" | "temporarily_unavailable" | "network_error" | "identity_error":
                    return "pending", None          # transient: retry until the device code expires
                case "slow_down":
                    return "slow_down", None
                case "expired_token":
                    return "expired", None
                case "authorization_declined" | "access_denied":
                    return "denied", None
                case _ if "AADSTS53003" in e.description or "AADSTS50097" in e.description:
                    return "policy_blocked", None   # Conditional Access blocks device-code sign-in
                case _:
                    return "failed", {"message": str(e)[:200]}   # e.g. a misconfigured app registration

    # PKCE redirect -----------------------------------------------------------------
    def authorize_url(self, state: str, challenge: str, redirect_uri: str) -> str:
        return f"{self.cfg.authority}/oauth2/v2.0/authorize?" + urlencode({
            "client_id": self.cfg.client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "response_mode": "query", "scope": AppConfig.SCOPES, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def redeem_code(self, code: str, verifier: str, redirect_uri: str) -> dict:
        return self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                            "code_verifier": verifier, "scope": AppConfig.SCOPES})

    @staticmethod
    def tenant_of(tokens: dict) -> str:
        """The tenant id from the id_token's claims (display only: no signature check needed)."""
        try:
            payload = tokens["id_token"].split(".")[1]
            return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("tid", "")
        except (KeyError, IndexError, ValueError):
            return ""


class Connector:
    """connect_start / connect_status, plus the redirect fallback's two endpoints."""

    BROWSER_SIGN_IN_S = 900                 # how long a browser sign-in link stays valid

    def __init__(self, auth: Auth, store, http, cfg: AppConfig, clock=time.time):
        self.auth, self.store, self.http, self.cfg, self.clock = auth, store, http, cfg, clock

    def start(self, method: str = "device") -> dict:
        d = self.auth.device_start()
        connect_id = secrets.token_urlsafe(16)
        self.store.create_connect(connect_id, {"device_code": d["device_code"], "interval": int(d.get("interval", 5)),
                                               "last_poll": 0.0}, expires_at=self.clock() + int(d["expires_in"]))
        out = {"connect_id": connect_id, "verification_uri": d["verification_uri"], "user_code": d["user_code"],
               "expires_in": int(d["expires_in"])}
        if self.cfg.public_url:     # offered up front: a tenant may refuse the code on Microsoft's page without telling us
            out["redirect_url"] = self._start_url(connect_id)
        return out

    def status(self, connect_id: str) -> dict:
        row = self.store.connect(connect_id)
        if row is None:
            return {"status": "unknown"}
        if row["status"] == "connected":
            return self._connected(connect_id)
        if row["status"] != "pending":
            return self._ended(connect_id, row["status"], row["data"].get("message"))
        if self.clock() > row["expires_at"]:
            return self._end(connect_id, "expired")
        data = row["data"]
        if data.get("browser"):                  # the user is signing in in the browser: no device polling
            return {"status": "pending"}
        if self.clock() - data["last_poll"] < data["interval"]:
            return {"status": "pending"}
        data["last_poll"] = self.clock()
        self.store.update_connect(connect_id, data)
        state, tokens = self.auth.device_poll(data["device_code"])
        match state:
            case "pending":
                return {"status": "pending"}
            case "slow_down":
                data["interval"] += 5
                self.store.update_connect(connect_id, data)
                return {"status": "pending"}
            case "connected":
                failure = self._save(connect_id, tokens)
                return self._ended(connect_id, "failed", failure) if failure else self._connected(connect_id)
            case "policy_blocked":
                return self._policy_blocked(connect_id)
            case "failed":
                return self._end(connect_id, "failed", tokens["message"])
            case other:
                return self._end(connect_id, other)

    def _end(self, connect_id: str, status: str, message: str | None = None) -> dict:
        """Record a terminal status. If a concurrent request already ended the connect (e.g. connected it),
        that outcome stands and is what this caller sees."""
        ended = self._fail(connect_id, message) if message else self.store.finish_connect(connect_id, status)
        return self._ended(connect_id, status, message) if ended else self.status(connect_id)

    def _ended(self, connect_id: str, status: str, message: str | None) -> dict:
        """expired / denied / failed. With OUTLOOK_PUBLIC_URL, the browser sign-in is offered as well: real
        Conditional Access does not always answer the device poll with AADSTS53003."""
        out = {"status": status, **({"message": message} if message else {})}
        if self.cfg.public_url:
            out["redirect_url"] = self._start_url(connect_id)
        return out

    def _policy_blocked(self, connect_id: str) -> dict:
        if not self.cfg.public_url:
            return {"status": "policy_blocked", "redirect_url": None,
                    "message": "Your organisation blocks code sign-in. Set OUTLOOK_PUBLIC_URL (https or localhost) "
                               "on the Outlook server to enable browser sign-in, or ask IT to allow device code."}
        return {"status": "policy_blocked", "redirect_url": self._start_url(connect_id)}

    def _start_url(self, connect_id: str) -> str:
        return f"{self.cfg.public_url}/auth/start?connect_id={connect_id}"

    def _save(self, connect_id: str, tokens: dict) -> str | None:
        """Store the new connection. If the account lookup fails twice, the connect is recorded as failed
        (the sign-in code is spent, so it cannot be retried) and the reason is returned."""
        from graph import GraphClient, GraphError, StaticToken
        graph = GraphClient(self.http, self.cfg.graph_base, StaticToken(tokens["access_token"]))
        for _ in range(2):
            try:
                me = graph.get("/me")
                break
            except (GraphError, httpx.HTTPError, ValueError) as e:
                code = getattr(e, "code", "network_error" if isinstance(e, httpx.HTTPError) else "graph_error")
        else:
            message = f"Signed in, but reading the account failed ({code}). Start again from the app."
            self._fail(connect_id, message)
            return message
        key = secrets.token_urlsafe(32)
        self.store.save_connection(key, me.get("mail") or me.get("userPrincipalName", ""),
                                   Auth.tenant_of(tokens), tokens["refresh_token"])
        self.store.finish_connect(connect_id, "connected", key)
        return None

    def _fail(self, connect_id: str, message: str) -> bool:
        """A terminal failure; later connect_status calls repeat the message. False if the connect had already ended."""
        if not self.store.finish_connect(connect_id, "failed"):
            return False
        self.store.update_connect(connect_id, {**self.store.connect(connect_id)["data"], "message": message})
        return True

    def _connected(self, connect_id: str) -> dict:
        key = self.store.take_connection_key(connect_id)
        if key is None:
            return {"status": "connected"}
        return {"status": "connected", "account": self.store.connection(key)["account"], "connection_key": key,
                "credential_header": CREDENTIAL_HEADER}

    # redirect fallback (PKCE) ------------------------------------------------------
    def _redirect_uri(self) -> str:
        return f"{self.cfg.public_url}/auth/callback"

    def redirect_start(self, connect_id: str) -> str:
        if not self.cfg.public_url:
            raise AuthError("no_public_url", "set OUTLOOK_PUBLIC_URL on the Outlook server to enable browser sign-in")
        row = self.store.connect(connect_id)
        if row is None or row["status"] == "connected":
            raise AuthError("unknown_connect", "start again from the app")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        # Pending again (also after expired / denied / failed), browser-only, for a fresh sign-in window.
        data = {k: v for k, v in row["data"].items() if k != "message"}
        if not self.store.reopen_connect(connect_id, {**data, "state": state, "verifier": verifier, "browser": True},
                                         expires_at=self.clock() + self.BROWSER_SIGN_IN_S):
            raise AuthError("unknown_connect", "start again from the app")
        return self.auth.authorize_url(state, challenge, self._redirect_uri())

    def redirect_callback(self, state: str, code: str) -> str:
        connect_id = self.store.connect_by_state(state)
        if connect_id is None:
            return "This sign-in link has expired. Start again from the app."
        row = self.store.connect(connect_id)
        try:
            tokens = self.auth.redeem_code(code, row["data"]["verifier"], self._redirect_uri())
        except AuthError as e:
            self.store.finish_connect(connect_id, "denied")
            return f"Sign-in failed: {e}"
        failure = self._save(connect_id, tokens)
        return f"Sign-in failed: {failure}" if failure else "Connected. You can close this tab and return to the app."
