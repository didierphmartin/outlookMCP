"""An httpx transport that plays Microsoft identity + Graph for tests and OUTLOOK_FAKE_GRAPH=1.

Implements only what this server calls. Every request is logged in .requests so tests can prove,
for example, that a poll never fetched attachment content.
"""
from __future__ import annotations

import base64
import itertools
import json
import time
from urllib.parse import parse_qs, quote, urlparse

import httpx

FOLDERS = [{"id": "inbox-id", "displayName": "Inbox", "wellKnown": "inbox"},
           {"id": "invoices-id", "displayName": "Invoices", "parent": "inbox-id"},
           {"id": "projects-id", "displayName": "Projects"}]


class FakeGraph:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.posted: list[tuple[str, dict]] = []  # (path, JSON body) of each POST to Graph
        self.folders = [dict(f) for f in FOLDERS]  # move_mail may create folders
        self.forbid_writes = False                # a connection consented before Mail.ReadWrite/Mail.Send: 403
        self.headers: list[dict[str, str]] = []   # each request's headers, in the order of .requests
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
        # The next delta request made from a delta link answers this status: 410 (SyncStateNotFound) or
        # 400 (SyncStateInvalid), as real Graph does when a delta link is too old or its sync state is gone.
        self.delta_link_failure: int | None = None
        # Change subscriptions: id -> {resource, clientState, expirationDateTime, folder}. A new message in a
        # subscribed folder calls on_notification with Graph's body, as the Event Hub listener would.
        self.subscriptions: dict[str, dict] = {}
        self.on_notification = None
        self.subscription_status: int | None = None   # POST /subscriptions answers this error status instead
        self.clock = time.time                         # "now" for the subscription expiry limit
        # How GET /me/messages/<id> writes parentFolderId (None: the folder id as is). Real Graph may answer it
        # in another id form than the mailFolders lookup.
        self.parent_folder_id_form = None
        # An organization (app-only) connection: several mailboxes and one group. The /me mailbox above is the
        # default (mailbox None); the others answer under users/{id}. An app token is refused on /me.
        self.group_id = "vip-group"
        self.group_name = "VIP Mailboxes"
        self.app_secret: str | None = None          # when set, client_credentials needs this client_secret
        self.mailboxes: dict[str, dict] = {}       # user id -> {mail, displayName, in_group}
        self.app_tokens: set[str] = set()
        self.forbidden_mailboxes: set[str] = set()
        self.group_page_size = 100
        self._box: str | None = None               # the mailbox the request in flight addresses (None: /me)
        self._removed_box: dict[str, str | None] = {}   # removed message id -> its mailbox

    # test controls -------------------------------------------------------------
    def add_mailbox(self, user_id: str, mail: str, display_name: str, in_group: bool = True) -> None:
        self.mailboxes[user_id] = {"mail": mail, "displayName": display_name, "in_group": in_group}

    def forbid_mailbox(self, user_id: str) -> None:
        self.forbidden_mailboxes.add(user_id)

    def add_message(self, folder="inbox", subject="", sender="a@b.c", received="2026-10-07T10:00:00Z",
                    attachments=(), body_html="<p>hi</p>", mailbox: str | None = None) -> str:
        seq = next(self._seq)
        mid = f"m{seq}"
        atts = []
        for i, (name, ctype, data, *flags) in enumerate(attachments):
            # flags: inline, item (attached email), contentId, reference (a cloud link: no content to download)
            inline, item, cid, ref = (list(flags) + [False, False, None, False])[:4]
            aid = f"{mid}-a{i}"
            self.attachments[aid] = data
            kind = "referenceAttachment" if ref else "itemAttachment" if item else "fileAttachment"
            atts.append({"@odata.type": "#microsoft.graph." + kind,
                         "id": aid, "name": name, "contentType": ctype, "size": len(data), "isInline": bool(inline),
                         **({"contentId": cid} if cid else {})})
        self.messages[mid] = {"id": mid, "seq": seq, "folder": self._folder_id(folder), "subject": subject,
                              "from": {"emailAddress": {"address": sender}},
                              "mailbox": mailbox,
                              "toRecipients": [{"emailAddress": {"address": self._mail(mailbox)}}], "ccRecipients": [],
                              "receivedDateTime": received, "hasAttachments": bool(atts),
                              "body": {"contentType": "html", "content": body_html},
                              "webLink": f"https://outlook.office.com/mail/id/{mid}", "attachments": atts}
        self._notify(mid)
        return mid

    def _mail(self, mailbox: str | None) -> str:
        return self.account if mailbox is None else self.mailboxes[mailbox]["mail"]

    def _notify(self, mid: str) -> None:
        folder, box = self.messages[mid]["folder"], self.messages[mid]["mailbox"]
        who = "u1" if box is None else box
        for sid, sub in list(self.subscriptions.items()):
            if sub["folder"] == folder and sub["mailbox"] == box and self.on_notification:
                self.on_notification({"value": [{"subscriptionId": sid, "clientState": sub["clientState"],
                                                 "changeType": "created", "tenantId": self.tenant,
                                                 "resource": f"Users/{who}/Messages/{mid}",
                                                 "resourceData": {"@odata.type": "#Microsoft.Graph.Message",
                                                                  "@odata.id": f"Users/{who}/Messages/{mid}",
                                                                  "id": mid}}]})

    def notify_created(self, mid: str) -> None:
        """Only the "created" notification of a message already in its folder: Graph sends this when an email
        is dragged out and back between two delta rounds, which the delta itself collapses into a change."""
        self._notify(mid)

    def lifecycle(self, sid: str, event: str, remove: bool = False) -> None:
        """Graph's lifecycle notification for a subscription (reauthorizationRequired, subscriptionRemoved, missed)."""
        sub = self.subscriptions.pop(sid) if remove else self.subscriptions[sid]
        if self.on_notification:
            self.on_notification({"value": [{"subscriptionId": sid, "clientState": sub["clientState"],
                                             "lifecycleEvent": event, "tenantId": self.tenant}]})

    def remove_message(self, mid: str) -> None:
        m = self.messages.pop(mid)
        self.removed.append((next(self._seq), m["folder"], mid))
        self._removed_box[mid] = m["mailbox"]

    def touch_message(self, mid: str) -> None:
        """Change a message (read, flagged, ...): like real Graph, the next delta round sends it again."""
        self.messages[mid]["seq"] = next(self._seq)

    def revoke_refresh(self) -> None:
        self.refresh_tokens.clear()

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    # transport -------------------------------------------------------------------
    def folder(self, fid: str) -> dict | None:
        return next((f for f in self.folders if f["id"] == fid), None)

    def _folder_id(self, name_or_id: str) -> str:
        for f in self.folders:
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
        self.headers.append(dict(request.headers))
        if url.path.endswith("/oauth2/v2.0/devicecode"):
            return httpx.Response(200, json={"device_code": "dc-1", "user_code": "K7PQ-M2XD",
                                             "verification_uri": "https://microsoft.com/devicelogin",
                                             "expires_in": 900, "interval": 5})
        if url.path.endswith("/oauth2/v2.0/token"):
            return self._token(parse_qs(request.content.decode()))
        if self.throttle_next:
            self.throttle_next -= 1
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"code": "TooManyRequests"}})
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        path = url.path.split("/v1.0", 1)[-1]
        first = path.strip("/").split("/")[0]
        if bearer not in self.access_tokens and not (bearer in self.app_tokens and first in ("users", "groups", "subscriptions")):
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        if (denied := self._select_mailbox(path)):
            return denied
        match request.method:
            case "POST":
                return self._post(path, json.loads(request.content or b"{}"))
            case "PATCH" | "DELETE":
                return self._subscription_change(request.method, path, json.loads(request.content or b"{}"))
        return self._graph(path, parse_qs(url.query))

    MAIL_MAX = 10070 * 60

    def _select_mailbox(self, path: str) -> httpx.Response | None:
        """users/{uid}/... addresses a mailbox like me/...: remember which one for the routes below."""
        parts = path.strip("/").split("/")
        self._box = None
        if parts[0] != "users" or len(parts) < 2:
            return None
        if parts[1] in self.forbidden_mailboxes:
            return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
        if parts[1] not in self.mailboxes:
            return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})
        self._box = parts[1]
        return None

    def _mine(self, parts: list[str]) -> list[str]:
        """The route table's form of a path: users/{uid}/x becomes me/x."""
        return ["me", *parts[2:]] if parts[0] == "users" and len(parts) > 1 else parts

    def _get(self, mid: str) -> dict | None:
        """A message of the addressed mailbox (another mailbox's message is not found)."""
        m = self.messages.get(mid)
        return m if m is not None and m["mailbox"] == self._box else None

    def _members(self, q: dict) -> httpx.Response:
        members = [{"id": uid, "mail": b["mail"], "displayName": b["displayName"]}
                   for uid, b in self.mailboxes.items() if b["in_group"]]
        skip = int(q.get("$skiptoken", ["0"])[0])
        body = {"value": members[skip:skip + self.group_page_size]}
        if skip + self.group_page_size < len(members):
            body["@odata.nextLink"] = (f"https://graph.microsoft.com/v1.0/groups/{self.group_id}"
                                       f"/members/microsoft.graph.user?$skiptoken={skip + self.group_page_size}")
        return httpx.Response(200, json=body)

    def _subscription_change(self, method: str, path: str, body: dict) -> httpx.Response:
        match path.strip("/").split("/"):
            case ["subscriptions", sid] if sid in self.subscriptions:
                if method == "DELETE":
                    del self.subscriptions[sid]
                    return httpx.Response(204)
                self.subscriptions[sid]["expirationDateTime"] = body["expirationDateTime"]
                return httpx.Response(200, json={"id": sid, **self.subscriptions[sid]})
            case ["subscriptions", _]:
                return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})
        return httpx.Response(404, json={"error": {"code": "NotImplementedInFake", "path": path}})

    def _subscribe(self, body: dict) -> httpx.Response:
        if self.subscription_status:
            return httpx.Response(self.subscription_status, json={"error": {"code": "ExtensionError", "message": "refused"}})
        import re
        from datetime import datetime
        m = re.fullmatch(r"(?:me|users/([^/]+))/mailFolders\('([^']+)'\)/messages", body.get("resource", ""))
        if not m or body.get("changeType") != "created":
            return httpx.Response(400, json={"error": {"code": "InvalidRequest", "message": "unsupported resource"}})
        box = m.group(1)
        if box is not None and box not in self.mailboxes:
            return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})
        if box in self.forbidden_mailboxes:
            return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
        exp = datetime.fromisoformat(body["expirationDateTime"].replace("Z", "+00:00")).timestamp()
        if exp - self.clock() > self.MAIL_MAX:
            return httpx.Response(400, json={"error": {"code": "ExtensionError",
                                                       "message": "Subscription expiration can only be 10070 minutes in the future."}})
        sid = f"sub-{next(self._seq)}"
        self.subscriptions[sid] = {"resource": body["resource"], "clientState": body.get("clientState", ""),
                                   "expirationDateTime": body["expirationDateTime"], "folder": self._folder_id(m.group(2)), "mailbox": box}
        return httpx.Response(201, json={"id": sid, **self.subscriptions[sid]})

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
            case "client_credentials":
                if self.app_secret is not None and form.get("client_secret", [""])[0] != self.app_secret:
                    return self._oauth_error("invalid_client", "AADSTS7000215: Invalid client secret provided.")
                if form.get("scope", [""])[0] != "https://graph.microsoft.com/.default":
                    return self._oauth_error("invalid_scope")
                at = f"APP{next(self._tokens)}"
                self.app_tokens.add(at)
                return httpx.Response(200, json={"access_token": at, "token_type": "Bearer", "expires_in": 3600})
            case "authorization_code":
                return self._tokens_response()
            case _:
                return self._oauth_error("unsupported_grant_type")

    # Properties that exist only on a subtype of microsoft.graph.attachment: real Graph rejects them in a
    # $select on the base type (top level or inside $expand=attachments(...)).
    SUBTYPE_ONLY = ("contentId", "contentBytes", "sourceUrl", "item")

    @classmethod
    def _bad_select(cls, q: dict) -> httpx.Response | None:
        import re
        selects = list(q.get("$select", []))
        for e in q.get("$expand", []):
            m = re.fullmatch(r"attachments\(\$select=([^)]*)\)", e)
            if m:
                selects.append(m.group(1))
        for sel in selects:
            for prop in sel.split(","):
                if prop.strip() in cls.SUBTYPE_ONLY:
                    return httpx.Response(400, json={"error": {"code": "BadRequest", "message":
                        f"Parsing OData Select and Expand failed: Could not find a property named '{prop.strip()}' "
                        "on type 'microsoft.graph.attachment'."}})
        return None

    @staticmethod
    def _expanded(att: dict, q: dict) -> dict:
        """An attachment as $expand=attachments($select=...) returns it: only the selected properties."""
        import re
        for e in q.get("$expand", []):
            m = re.fullmatch(r"attachments\(\$select=([^)]*)\)", e)
            if m:
                keep = set(m.group(1).split(",")) | {"@odata.type"}
                return {k: v for k, v in att.items() if k in keep}
        return att

    def _graph(self, path: str, q: dict) -> httpx.Response:
        if (bad := self._bad_select(q)):
            return bad
        parts = path.strip("/").split("/")
        if parts == ["groups", self.group_id]:
            return httpx.Response(200, json={"id": self.group_id, "displayName": self.group_name})
        if parts == ["groups", self.group_id, "members", "microsoft.graph.user"]:
            return self._members(q)
        parts = self._mine(parts)
        match parts:
            case ["me"] if self._box:
                b = self.mailboxes[self._box]
                return httpx.Response(200, json={"id": self._box, "mail": b["mail"], "userPrincipalName": b["mail"]})
            case ["me"]:
                return httpx.Response(200, json={"mail": self.account, "userPrincipalName": self.account})
            case ["me", "mailFolders"]:
                return httpx.Response(200, json={"value": [f for f in self.folders if "parent" not in f]})
            case ["me", "mailFolders", fid, "childFolders"]:
                return httpx.Response(200, json={"value": [f for f in self.folders if f.get("parent") == self._folder_id(fid)]})
            case ["me", "mailFolders", fid] if self.folder_name_status and any(
                    fid == f["displayName"] and fid not in (f["id"], f.get("wellKnown")) for f in self.folders):
                return httpx.Response(self.folder_name_status, json={"error": {"code": "ErrorInvalidIdMalformed"}})
            case ["me", "mailFolders", fid]:
                fid = self._folder_id(fid)
                f = self.folder(fid)
                return httpx.Response(200, json=f) if f else httpx.Response(404, json={"error": {"code": "ErrorFolderNotFound"}})
            case ["me", "mailFolders", fid, "messages", "delta"]:
                return self._delta(self._folder_id(fid), q)
            case ["me", "mailFolders", fid, "messages", mid]:
                m = self._get(mid)
                if m is None or m["folder"] != self._folder_id(fid):
                    return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
                return httpx.Response(200, json={"id": mid})
            case ["me", "messages", mid, "attachments", aid, "$value"]:
                att = next((a for a in (self._get(mid) or {}).get("attachments", []) if a["id"] == aid), {})
                if att.get("@odata.type") == "#microsoft.graph.referenceAttachment":    # a link: nothing to download
                    return httpx.Response(400, json={"error": {"code": "ErrorInvalidRequest"}})
                return httpx.Response(200, content=self.attachments[aid])
            case ["me", "messages", mid, "attachments", aid]:
                m = self._get(mid)
                att = next((a for a in (m or {}).get("attachments", []) if a["id"] == aid), None)
                if not att:
                    return httpx.Response(404, json={})
                full = {**att, "contentBytes": base64.b64encode(self.attachments[aid]).decode()} \
                    if att["@odata.type"].endswith("fileAttachment") else att
                return httpx.Response(200, json=full)
            case ["me", "messages", mid]:
                m = self._get(mid)
                if m is None:
                    return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
                form = self.parent_folder_id_form or (lambda fid: fid)
                out = {**m, "parentFolderId": form(m["folder"])}
                out["attachments"] = [self._expanded(a, q) for a in m["attachments"]]
                return httpx.Response(200, json=out)
        return httpx.Response(404, json={"error": {"code": "NotImplementedInFake", "path": path}})

    def _post(self, path: str, body: dict) -> httpx.Response:
        self.posted.append((path, body))
        if path.strip("/") == "subscriptions":            # needs only Mail.Read: not a write
            return self._subscribe(body)
        if self.forbid_writes:
            return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
        match self._mine(path.strip("/").split("/")):
            case ["me", "messages", mid, "move"]:
                m, dest = self._get(mid), self.folder(self._folder_id(body.get("destinationId", "")))
                if m is None or dest is None:
                    return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
                self.removed.append((next(self._seq), m["folder"], mid))   # the source folder's delta: gone
                self._removed_box[mid] = m["mailbox"]
                m["folder"], m["seq"] = dest["id"], next(self._seq)        # immutable id: same id after the move
                self._notify(mid)                                          # "created" in the destination folder
                return httpx.Response(201, json=m)
            case ["me", "messages", mid, "forward"]:
                if self._get(mid) is None:
                    return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
                return httpx.Response(202)
            case ["me", "mailFolders"] | ["me", "mailFolders", _, "childFolders"] as parts:
                f = {"id": f"folder-{next(self._seq)}", "displayName": body["displayName"]}
                if len(parts) == 4:
                    f["parent"] = self._folder_id(parts[2])
                self.folders.append(f)
                return httpx.Response(201, json=f)
        return httpx.Response(404, json={"error": {"code": "NotImplementedInFake", "path": path}})

    def _delta(self, fid: str, q: dict) -> httpx.Response:
        """Like real Graph: pages are not oldest-first (newest-first here), changed messages come again,
        and the nextLink/deltaLink keep the original $filter."""
        if self.delta_link_failure and "$deltatoken" in q:
            status, self.delta_link_failure = self.delta_link_failure, None
            code = "SyncStateNotFound" if status == 410 else "SyncStateInvalid"
            return httpx.Response(status, json={"error": {"code": code, "message": f"{code}: resync required"}})
        after = int(q.get("$deltatoken", ["0"])[0])
        since = q.get("$filter", ["receivedDateTime ge "])[0].removeprefix("receivedDateTime ge ")
        skip = int(q.get("$skiptoken", ["0"])[0])
        changes = [m for m in sorted(self.messages.values(), key=lambda m: m["seq"], reverse=True)
                   if m["folder"] == fid and m["mailbox"] == self._box and m["seq"] > after and m["receivedDateTime"] >= since]
        changes = [{k: v for k, v in m.items() if k in ("id", "from", "subject", "receivedDateTime", "hasAttachments")}
                   for m in changes]
        if "$deltatoken" in q:                            # like real Graph, a first (token-less) round lists no removals
            changes += [{"id": mid, "@removed": {"reason": "deleted"}} for seq, f, mid in self.removed
                        if f == fid and seq > after and self._removed_box.get(mid) == self._box]
        page, rest = changes[skip:skip + 2], changes[skip + 2:]
        who = "me" if self._box is None else f"users/{self._box}"
        base = f"https://graph.microsoft.com/v1.0/{who}/mailFolders/{fid}/messages/delta"
        keep_filter = "&$filter=" + quote(f"receivedDateTime ge {since}") if since else ""
        body = {"value": page}
        if rest:
            body["@odata.nextLink"] = f"{base}?$deltatoken={after}&$skiptoken={skip + 2}{keep_filter}"
        else:
            top = max([m["seq"] for m in self.messages.values()] + [s for s, _, _ in self.removed] + [after])
            body["@odata.deltaLink"] = f"{base}?$deltatoken={top}{keep_filter}"
        return httpx.Response(200, json=body)
