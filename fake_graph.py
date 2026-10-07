"""An httpx transport that plays Microsoft identity + Graph for tests and OUTLOOK_FAKE_GRAPH=1.

Implements only what this server calls. Every request is logged in .requests so tests can prove,
for example, that a poll never fetched attachment content.
"""
from __future__ import annotations

import base64
import itertools
import json
from urllib.parse import parse_qs, quote, urlparse

import httpx

FOLDERS = [{"id": "inbox-id", "displayName": "Inbox", "wellKnown": "inbox"},
           {"id": "invoices-id", "displayName": "Invoices", "parent": "inbox-id"},
           {"id": "projects-id", "displayName": "Projects"}]


class FakeGraph:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.messages: dict[str, dict] = {}       # id -> message (with folder, seq)
        self.removed: list[tuple[int, str, str]] = []   # (seq, folder, id)
        self.attachments: dict[str, bytes] = {}   # attachment id -> bytes
        self._seq = itertools.count(1)
        self._tokens = itertools.count(1)
        self.access_tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.device_state = "pending"
        self.throttle_next = 0
        # How GET /me/mailFolders/<displayName> answers: None resolves it (default); 404 or 400 refuse it,
        # because real Graph may reject an unknown well-known-folder name with 400. Ids and well-known names still work.
        self.folder_name_status: int | None = None
        self.account, self.tenant = "me@contoso.com", "tid-1"

    # test controls -------------------------------------------------------------
    def add_message(self, folder="inbox", subject="", sender="a@b.c", received="2026-10-07T10:00:00Z",
                    attachments=(), body_html="<p>hi</p>") -> str:
        seq = next(self._seq)
        mid = f"m{seq}"
        atts = []
        for i, (name, ctype, data) in enumerate(attachments):
            aid = f"{mid}-a{i}"
            self.attachments[aid] = data
            atts.append({"id": aid, "name": name, "contentType": ctype, "size": len(data)})
        self.messages[mid] = {"id": mid, "seq": seq, "folder": self._folder_id(folder), "subject": subject,
                              "from": {"emailAddress": {"address": sender}},
                              "toRecipients": [{"emailAddress": {"address": self.account}}], "ccRecipients": [],
                              "receivedDateTime": received, "hasAttachments": bool(atts),
                              "body": {"contentType": "html", "content": body_html},
                              "webLink": f"https://outlook.office.com/mail/id/{mid}", "attachments": atts}
        return mid

    def remove_message(self, mid: str) -> None:
        m = self.messages.pop(mid)
        self.removed.append((next(self._seq), m["folder"], mid))

    def touch_message(self, mid: str) -> None:
        """Change a message (read, flagged, ...): like real Graph, the next delta round sends it again."""
        self.messages[mid]["seq"] = next(self._seq)

    def revoke_refresh(self) -> None:
        self.refresh_tokens.clear()

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    # transport -------------------------------------------------------------------
    @staticmethod
    def _folder_id(name_or_id: str) -> str:
        for f in FOLDERS:
            if name_or_id in (f["id"], f.get("wellKnown"), f["displayName"]):
                return f["id"]
        return name_or_id

    def _tokens_response(self) -> httpx.Response:
        at, rt = f"AT{next(self._tokens)}", f"RT{next(self._tokens)}"
        self.access_tokens.add(at)
        self.refresh_tokens.add(rt)
        claims = base64.urlsafe_b64encode(json.dumps({"tid": self.tenant}).encode()).decode().rstrip("=")
        return httpx.Response(200, json={"access_token": at, "refresh_token": rt, "expires_in": 3600,
                                         "id_token": f"h.{claims}.s"})

    @staticmethod
    def _oauth_error(code: str, desc: str = "") -> httpx.Response:
        return httpx.Response(400, json={"error": code, "error_description": desc})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        self.requests.append((request.method, url.path + (f"?{url.query}" if url.query else "")))
        if url.path.endswith("/oauth2/v2.0/devicecode"):
            return httpx.Response(200, json={"device_code": "dc-1", "user_code": "K7PQ-M2XD",
                                             "verification_uri": "https://microsoft.com/devicelogin",
                                             "expires_in": 900, "interval": 5})
        if url.path.endswith("/oauth2/v2.0/token"):
            return self._token(parse_qs(request.content.decode()))
        if self.throttle_next:
            self.throttle_next -= 1
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"code": "TooManyRequests"}})
        if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self.access_tokens:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return self._graph(url.path.split("/v1.0", 1)[-1], parse_qs(url.query))

    def _token(self, form: dict) -> httpx.Response:
        match form.get("grant_type", [""])[0]:
            case "urn:ietf:params:oauth:grant-type:device_code":
                match self.device_state:
                    case "pending":
                        return self._oauth_error("authorization_pending")
                    case "blocked":
                        return self._oauth_error("invalid_grant", "AADSTS53003: Access has been blocked by Conditional Access policies.")
                    case "declined":
                        return self._oauth_error("authorization_declined")
                    case _:
                        return self._tokens_response()
            case "refresh_token":
                if form.get("refresh_token", [""])[0] not in self.refresh_tokens:
                    return self._oauth_error("invalid_grant", "AADSTS70008: The refresh token has expired.")
                return self._tokens_response()
            case "authorization_code":
                return self._tokens_response()
            case _:
                return self._oauth_error("unsupported_grant_type")

    def _graph(self, path: str, q: dict) -> httpx.Response:
        parts = path.strip("/").split("/")
        match parts:
            case ["me"]:
                return httpx.Response(200, json={"mail": self.account, "userPrincipalName": self.account})
            case ["me", "mailFolders"]:
                return httpx.Response(200, json={"value": [f for f in FOLDERS if "parent" not in f]})
            case ["me", "mailFolders", fid, "childFolders"]:
                return httpx.Response(200, json={"value": [f for f in FOLDERS if f.get("parent") == self._folder_id(fid)]})
            case ["me", "mailFolders", fid] if self.folder_name_status and any(
                    fid == f["displayName"] and fid not in (f["id"], f.get("wellKnown")) for f in FOLDERS):
                return httpx.Response(self.folder_name_status, json={"error": {"code": "ErrorInvalidIdMalformed"}})
            case ["me", "mailFolders", fid]:
                fid = self._folder_id(fid)
                f = next((f for f in FOLDERS if f["id"] == fid), None)
                return httpx.Response(200, json=f) if f else httpx.Response(404, json={"error": {"code": "ErrorFolderNotFound"}})
            case ["me", "mailFolders", fid, "messages", "delta"]:
                return self._delta(self._folder_id(fid), q)
            case ["me", "messages", mid, "attachments", aid, "$value"]:
                return httpx.Response(200, content=self.attachments[aid])
            case ["me", "messages", mid, "attachments", aid]:
                m = self.messages.get(mid)
                att = next((a for a in (m or {}).get("attachments", []) if a["id"] == aid), None)
                return httpx.Response(200, json=att) if att else httpx.Response(404, json={})
            case ["me", "messages", mid]:
                m = self.messages.get(mid)
                return httpx.Response(200, json=m) if m else httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        return httpx.Response(404, json={"error": {"code": "NotImplementedInFake", "path": path}})

    def _delta(self, fid: str, q: dict) -> httpx.Response:
        """Like real Graph: pages are not oldest-first (newest-first here), changed messages come again,
        and the nextLink/deltaLink keep the original $filter."""
        after = int(q.get("$deltatoken", ["0"])[0])
        since = q.get("$filter", ["receivedDateTime ge "])[0].removeprefix("receivedDateTime ge ")
        skip = int(q.get("$skiptoken", ["0"])[0])
        changes = [m for m in sorted(self.messages.values(), key=lambda m: m["seq"], reverse=True)
                   if m["folder"] == fid and m["seq"] > after and m["receivedDateTime"] >= since]
        changes = [{k: v for k, v in m.items() if k in ("id", "from", "subject", "receivedDateTime", "hasAttachments")}
                   for m in changes]
        changes += [{"id": mid, "@removed": {"reason": "deleted"}} for seq, f, mid in self.removed if f == fid and seq > after]
        page, rest = changes[skip:skip + 2], changes[skip + 2:]
        base = f"https://graph.microsoft.com/v1.0/me/mailFolders/{fid}/messages/delta"
        keep_filter = "&$filter=" + quote(f"receivedDateTime ge {since}") if since else ""
        body = {"value": page}
        if rest:
            body["@odata.nextLink"] = f"{base}?$deltatoken={after}&$skiptoken={skip + 2}{keep_filter}"
        else:
            top = max([m["seq"] for m in self.messages.values()] + [s for s, _, _ in self.removed] + [after])
            body["@odata.deltaLink"] = f"{base}?$deltatoken={top}{keep_filter}"
        return httpx.Response(200, json=body)
