"""The mailbox tools. poll_new_mail never touches attachment content (spec §5.1)."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from cryptography.fernet import InvalidToken

from auth import AuthError
from extract import attachment_text, body_text
from graph import GraphError
from mailboxes import Mailbox, MailboxDirectory
from pdf import render_mail

DELTA_SELECT = "id,from,subject,receivedDateTime,hasAttachments"
FULL_SELECT = "id,from,toRecipients,ccRecipients,subject,receivedDateTime,body,webLink,hasAttachments,parentFolderId"
ITEM_ATTACHMENT = "#microsoft.graph.itemAttachment"
REFERENCE_ATTACHMENT = "#microsoft.graph.referenceAttachment"      # a cloud link (OneDrive/SharePoint): no content
ATTACHMENT_KINDS = {ITEM_ATTACHMENT: "item", REFERENCE_ATTACHMENT: "reference"}   # anything else is a file
ATTACHMENT_META = "attachments($select=id,name,contentType,size,isInline)"   # metadata only, never contentBytes
EMAIL = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+")

log = logging.getLogger("outlook_mcp.mail")


class Message:
    """Graph message dicts -> the tool's mail shape."""

    @staticmethod
    def address(recipient: dict | None) -> str:
        return ((recipient or {}).get("emailAddress") or {}).get("address", "")

    @classmethod
    def shape(cls, m: dict, attempt: int = 1) -> dict:
        return {"id": m["id"], "attempt": attempt, "from": cls.address(m.get("from")),
                "to": [cls.address(r) for r in m.get("toRecipients") or []],
                "cc": [cls.address(r) for r in m.get("ccRecipients") or []],
                "subject": m.get("subject") or "", "received_at": m.get("receivedDateTime"),
                "body_text": body_text(m.get("body")), "web_link": m.get("webLink") or "",
                "attachments": [{"id": a["id"], "name": a.get("name", ""), "content_type": a.get("contentType", ""),
                                 "size": int(a.get("size") or 0),
                                 "is_inline": bool(a.get("isInline")),
                                 "kind": ATTACHMENT_KINDS.get(a.get("@odata.type"), "file")}
                                for a in m.get("attachments") or []]}


MODES = ("new", "drop")


@dataclass(frozen=True)
class Filters:
    sender: str = ""
    subject_contains: str = ""
    has_attachments: bool = False
    mode: str = "new"       # new: mail received after the watch started; drop: any email moved into the folder

    @classmethod
    def from_args(cls, args: dict) -> "Filters":
        mode = str(args.get("mode") or "new").strip().lower()
        if mode not in MODES:
            raise GraphError("invalid_argument", f"mode must be new or drop, not {args.get('mode')!r}")
        return cls(str(args.get("from") or "").strip().lower(), str(args.get("subject_contains") or "").strip().lower(),
                   cls._flag(args.get("has_attachments")), mode)

    @property
    def drop(self) -> bool:
        return self.mode == "drop"

    @staticmethod
    def _flag(value) -> bool:
        match value:
            case str():
                return value.strip().lower() not in ("", "false", "0", "no", "off")
            case _:
                return bool(value)

    def signature(self) -> str:
        """The scope's filter part. "new" leaves the mode out, so scopes made before modes existed keep their queue."""
        fields = asdict(self)
        if not self.drop:
            del fields["mode"]
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]

    def match(self, m: dict) -> bool:
        return ((not self.sender or self.sender in Message.address(m.get("from")).lower())
                and (not self.subject_contains or self.subject_contains in (m.get("subject") or "").lower())
                and (not self.has_attachments or bool(m.get("hasAttachments"))))


class MailService:
    def __init__(self, graph_for, store, cfg, now_iso=None, clock=time.time):
        self.graph_for, self.store, self.cfg, self.clock = graph_for, store, cfg, clock
        self.now_iso = now_iso or self.utc_now
        self._graphs: dict = {}          # connection key -> GraphClient (one TokenSource per connection)
        self._orgs: dict = {}            # connection key -> is it an organization connection
        self._directories: dict = {}     # organization connection key -> MailboxDirectory
        self._graphs_lock = threading.Lock()

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def iso(epoch: float) -> str:
        """A stored clock reading (epoch seconds) in Graph's receivedDateTime form."""
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _graph(self, key: str):
        with self._graphs_lock:
            if key not in self._graphs:
                self._graphs[key] = self.graph_for(key)
            return self._graphs[key]

    # organization connections: every mailbox of one group -----------------------------
    def _is_org(self, key: str) -> bool:
        with self._graphs_lock:
            if key not in self._orgs:
                conn = self.store.connection(key)
                self._orgs[key] = bool(conn) and conn["kind"] == "organization"
            return self._orgs[key]

    def _root(self, key: str, mailbox_id: str | None) -> str:
        """The Graph path prefix of a mailbox: /me for a user connection, /users/{id} for an organization's."""
        return f"/users/{quote(mailbox_id or '', safe='')}" if self._is_org(key) else "/me"

    def directory(self, key: str) -> MailboxDirectory:
        """The group's mailboxes. A mailbox that left it has its queue rows and cursors forgotten at the re-read."""
        graph = self._graph(key)
        with self._graphs_lock:
            if key not in self._directories:
                d = self._directories[key] = MailboxDirectory(graph, self.cfg.org_group, self.clock)
                d.on_departed(lambda gone: self._forget_mailboxes(key, gone))
            return self._directories[key]

    def _forget_mailboxes(self, key: str, gone: list[Mailbox]) -> None:
        for box in gone:
            n = self.store.purge_scope_prefix(key, f"{box.id}|")
            log.info("mailbox %s (%s) left the group: %d queued email(s) forgotten", box.mail, box.id, n)

    def _split(self, key: str, mail_id: str) -> tuple[str | None, str]:
        """An organization's mail id "{mailbox id}~{Graph id}" -> (mailbox id, Graph id); a user's: (None, the id).
        A mailbox no longer in the group is refused here, before Graph is asked."""
        if not self._is_org(key):
            return None, mail_id
        box, sep, mid = mail_id.partition("~")
        if not (sep and box and mid):
            raise GraphError("invalid_argument", "not an email id of this organization connection (mailbox~id)")
        if box not in {m.id for m in self.directory(key).members()}:
            raise GraphError("forbidden", "the email's mailbox is no longer in the organization's group")
        return box, mid

    # folders -------------------------------------------------------------------------
    def _folder_id(self, graph, folder: str, created: list[str] | None = None, root: str = "/me") -> str:
        """'Inbox', a well-known name, an id, a top-level display name, or a path 'Parent/Child' -> the folder id.
        With `created` (a list), missing folders along the path are created and their paths appended to it."""
        parts = [p for p in (folder or "Inbox").split("/") if p] or ["Inbox"]
        try:
            current = graph.get(f"{root}/mailFolders/{quote(parts[0].replace(' ', ''), safe='')}")["id"]
        except GraphError as e:
            match e.code:
                case "not_found" | "graph_error":   # 404, or 400 for a name that is not well-known: look it up by name
                    current = self._child(graph, None, parts[0], created, parts[0], root)
                case _:
                    raise
        for i, name in enumerate(parts[1:], 2):
            current = self._child(graph, current, name, created, "/".join(parts[:i]), root)
        return current

    @staticmethod
    def _child(graph, parent: str | None, name: str, created: list[str] | None = None, path: str = "",
               root: str = "/me") -> str:
        folders = f"{root}/mailFolders/{parent}/childFolders" if parent else f"{root}/mailFolders"
        for f in graph.get(folders, {"$top": "100"}).get("value", []):
            if f["displayName"].lower() == name.lower():
                return f["id"]
        if created is None:
            raise GraphError("unknown_folder", name)
        created.append(path)
        return graph.post(folders, {"displayName": name})["id"]

    # queue ---------------------------------------------------------------------------
    def _refresh_queue(self, graph, key: str, scope: str, folder_id: str, filters: Filters, root: str = "/me",
                       first: bool | None = None) -> None:
        """Read the folder's delta since the cursor and queue the matching new messages. The queue (not the
        delta order) decides delivery order; re-sent (changed) messages are already queued and stay ignored.
        `first`: is this the connection's first watch (None: it is when the connection has no cursor yet; an
        organization passes "no poll round has reached every mailbox yet")."""
        if filters.drop:
            return self._refresh_drop_queue(graph, key, scope, folder_id, filters, root, first)
        cur = self.store.cursor(key, scope)
        if cur is None:
            # First use of this folder + filter set. The connection's first one starts at the sign-in, so mail
            # sent after Connect counts; a later one starts "now", so a changed filter replays nothing.
            # Saved before Graph is asked, so a failed first round does not move it forward.
            first = not self.store.has_cursor(key) if first is None else first
            cur = {"delta_link": None, "since": self.iso(self.store.connection(key)["connected_at"]) if first else self.now_iso()}
            self.store.save_cursor(key, scope, None, cur["since"])
        since = cur["since"]
        fresh = (f"{root}/mailFolders/{folder_id}/messages/delta",
                 {"$filter": f"receivedDateTime ge {since}", "$select": DELTA_SELECT})
        try:
            found, delta_link = self._read_delta(graph, *((cur["delta_link"], None) if cur["delta_link"] else fresh),
                                                 since, filters)
        except GraphError as e:
            if e.code != "sync_state_lost" or not cur["delta_link"]:
                raise
            # The delta link is stale (410 / SyncState*): start again from the newest mail already queued, not
            # from `since`, so months of handled mail are not paged through again. That newest one (and any
            # other at the same second) comes back too, and the queue ignores it, so nothing is delivered twice.
            rescan = max(since, self.store.newest_received(key, scope) or since)
            found, delta_link = self._read_delta(graph, fresh[0], {**fresh[1], "$filter": f"receivedDateTime ge {rescan}"},
                                                 rescan, filters)
        self.store.enqueue(key, scope, found)       # before the cursor: a crash re-reads, never loses
        self.store.save_cursor(key, scope, delta_link, since)

    def _refresh_drop_queue(self, graph, key: str, scope: str, folder_id: str, filters: Filters, root: str = "/me",
                            first: bool | None = None) -> None:
        """Drop mode: whatever the received date, a matching email that comes into the folder is queued; one that
        leaves it (@removed) is forgotten, so dragging it back queues it again. The first round only records what
        is already there (the baseline): it is never delivered. With `first` given (an organization), only scopes
        first read before a round reached every mailbox have a baseline: a mailbox seen later has all its folder
        delivered."""
        cur = self.store.cursor(key, scope)
        fresh_round = cur is None or not cur["delta_link"]    # no delta round has completed yet
        baseline = fresh_round if first is None else fresh_round and first
        fresh = (f"{root}/mailFolders/{folder_id}/messages/delta", {"$select": DELTA_SELECT})
        removed: list[str] = []
        try:
            found, delta_link = self._read_delta(graph, *((cur["delta_link"], None) if not fresh_round else fresh),
                                                 "", filters, removed)
        except GraphError as e:
            if e.code != "sync_state_lost" or fresh_round:
                raise
            # A stale delta link: rescan the whole folder. Rows of emails still there (the baseline included) are
            # kept as they are, so nothing is delivered twice; only emails not queued yet are added. Rows of emails
            # that left during the gap are forgotten (their @removed is lost), so dragging one back queues it again.
            found, delta_link = self._read_delta(graph, *fresh, "", filters)
            removed = self.store.queued_except(key, scope, {mid for mid, _ in found})
        self.store.forget(key, scope, removed)
        self.store.enqueue_drop(key, scope, found, baseline=baseline)   # before the cursor: a crash re-reads
        self.store.save_cursor(key, scope, delta_link, cur["since"] if cur else self.now_iso())

    @staticmethod
    def _read_delta(graph, url: str, params: dict | None, since: str, filters: Filters,
                    removed: list[str] | None = None) -> tuple[list, str]:
        """Every page of one delta round -> ([(id, receivedDateTime) of matching new mail], the next delta link).
        Ids that left the folder are appended to `removed` when it is given."""
        found = []
        while True:
            page = graph.get(url, params)
            params = None
            for m in page.get("value", []):
                if "@removed" in m:
                    if removed is not None:
                        removed.append(m["id"])
                elif (m.get("receivedDateTime") or "") >= since and filters.match(m):
                    found.append((m["id"], m["receivedDateTime"]))
            if "@odata.nextLink" not in page:
                return found, page["@odata.deltaLink"]
            url = page["@odata.nextLink"]

    def _watched_folder(self, graph, args: dict, filters: Filters, root: str = "/me") -> str:
        # A drop folder is created on first use (the user drags mail into it); a "new" watch names an existing one.
        folder = str(args.get("folder") or "Inbox").strip().strip("/")
        return self._folder_id(graph, folder, [] if filters.drop else None, root)

    def _scope(self, graph, args: dict) -> tuple[str, str, Filters]:
        filters = Filters.from_args(args)
        folder_id = self._watched_folder(graph, args, filters)
        return f"{folder_id}|{filters.signature()}", folder_id, filters

    def _org_scope(self, graph, key: str, box: Mailbox, args: dict, filters: Filters) -> tuple[str, str]:
        """(queue scope, folder id) of one organization mailbox: the scope starts with the mailbox id."""
        folder_id = self._watched_folder(graph, args, filters, self._root(key, box.id))
        return f"{box.id}|{folder_id}|{filters.signature()}", folder_id

    def watch_scope(self, key: str, args: dict) -> tuple[str, str, str]:
        """(queue scope, folder id, mode) of poll_new_mail with these arguments: what wait_for_new watches."""
        scope, folder_id, filters = self._scope(self._graph(key), args)
        return scope, folder_id, filters.mode

    def lease_s(self, mode: str) -> float | None:
        """The claim lease of a mode: drop re-delivers an email still in the folder; new is at-most-once."""
        return self.cfg.drop_lease_s if mode == "drop" else None

    def _refresh_queue_for_tests(self, key: str, folder: str, args: dict) -> None:
        graph = self._graph(key)
        scope, folder_id, filters = self._scope(graph, {**args, "folder": folder})
        self._refresh_queue(graph, key, scope, folder_id, filters)

    # tools ---------------------------------------------------------------------------
    def poll_new_mail(self, key: str, args: dict) -> dict:
        try:
            if self._is_org(key):
                return self._poll_organization(key, args)
            graph = self._graph(key)
            scope, folder_id, filters = self._scope(graph, args)
            self._refresh_queue(graph, key, scope, folder_id, filters)
            while (mid := self.store.claim_next(key, scope, self.lease_s(filters.mode))) is not None:
                if (m := self._claimed(graph, key, scope, folder_id, filters, mid, self._root(key, None))) is not None:
                    return {"mail": self._shape(key, scope, filters, mid, m)}
            return {"mail": None}
        except Exception as e:                                      # the tool never raises
            return self._failure(e)

    def _poll_organization(self, key: str, args: dict) -> dict:
        """Every member mailbox's folder is refreshed, then the oldest email of them all is claimed. One mailbox's
        failure (403/404 out of scope, a missing folder, throttling, the network) is logged and that mailbox
        skipped; the others go on. Raised only when it is the connection's (reconnect_needed) or every one failed."""
        graph, filters = self._graph(key), Filters.from_args(args)
        # Until a round has reached every mailbox, what is already in a newly read folder is the baseline.
        first = not self.store.first_round_done(key)
        members = self.directory(key).members()
        watched: dict[str, tuple[Mailbox, str]] = {}  # scope -> (mailbox, folder id)
        failed: list[Exception] = []
        for box in members:
            try:
                scope, folder_id = self._org_scope(graph, key, box, args, filters)
                self._refresh_queue(graph, key, scope, folder_id, filters, self._root(key, box.id), first)
            except (GraphError, httpx.HTTPError, ValueError) as e:
                if isinstance(e, GraphError) and e.code == "reconnect_needed":
                    raise
                failed.append(e)
                log.warning("mailbox %s (%s) skipped: %s", box.mail, box.id, self._failure(e))
                continue
            watched[scope] = (box, folder_id)
        if failed and not watched:
            raise failed[0]
        if first and watched and not failed:        # an empty group has reached nobody yet
            self.store.mark_first_round(key)
        while (got := self.store.claim_next_of(key, list(watched), self.lease_s(filters.mode))) is not None:
            scope, mid = got
            box, folder_id = watched[scope]
            if (m := self._claimed(graph, key, scope, folder_id, filters, mid, self._root(key, box.id))) is not None:
                mail = self._shape(key, scope, filters, mid, m)
                return {"mail": {**mail, "id": f"{box.id}~{mid}", "mailbox": box.mail, "mailbox_name": box.name}}
        return {"mail": None}

    def _shape(self, key: str, scope: str, filters: Filters, mid: str, m: dict) -> dict:
        """A shape failure is permanent: the claimed message counts as consumed."""
        return Message.shape(m, self.store.attempts(key, scope, mid)) if filters.drop else Message.shape(m)

    def _claimed(self, graph, key: str, scope: str, folder_id: str, filters: Filters, mid: str, root: str) -> dict | None:
        """The claimed message from Graph, or None when it is gone (deleted, or left the drop folder): claim the next."""
        try:
            m = graph.get(f"{root}/messages/{mid}", {"$select": FULL_SELECT, "$expand": ATTACHMENT_META})
        except Exception as e:
            match e:
                case GraphError(code="not_found"):
                    if filters.drop:
                        self.store.forget(key, scope, [mid])
                    return None                               # deleted meanwhile: skip it
                case GraphError() if e.transient or e.code == "reconnect_needed":
                    self.store.release(key, scope, mid)       # never handed out: offer it next poll
                case httpx.HTTPError() | ValueError():        # the network, or a non-JSON gateway page
                    self.store.release(key, scope, mid)
            raise                                             # permanent: the message counts as consumed
        if filters.drop and m.get("parentFolderId") != folder_id \
                and not self._still_in(graph, key, scope, folder_id, mid, root):
            self.store.forget(key, scope, [mid])              # left the drop folder since the last delta round
            return None
        return m

    def _still_in(self, graph, key: str, scope: str, folder_id: str, mid: str, root: str = "/me") -> bool:
        """Is the message in the folder? Asked of the folder itself, so the answer does not depend on the id
        form Graph writes parentFolderId in. A failure other than 404 leaves the claim unspent and is raised."""
        try:
            graph.get(f"{root}/mailFolders/{folder_id}/messages/{quote(mid, safe='')}", {"$select": "id"})
            return True
        except GraphError as e:
            if e.code == "not_found":
                return False
            self.store.release(key, scope, mid)
            raise
        except (httpx.HTTPError, ValueError):
            self.store.release(key, scope, mid)
            raise

    @staticmethod
    def _failure(e: Exception) -> dict:
        """Any failure -> the {"error": code, "message": ...} every tool returns."""
        match e:
            case GraphError():
                return {"error": e.code, "message": e.message}
            case AuthError():
                g = GraphError.from_auth(e)
                return {"error": g.code, "message": g.message}
            case httpx.HTTPError():
                return {"error": "network_error", "message": f"{type(e).__name__}: {e}"[:200]}
            case InvalidToken():                                    # OUTLOOK_SECRET_KEY changed since the sign-in
                return {"error": "reconnect_needed",
                        "message": "the stored sign-in cannot be decrypted (server key changed): connect the mailbox again"}
            case ValueError():                                      # a non-JSON answer from Graph
                return {"error": "graph_error", "message": str(e)[:200]}
            case _:
                return {"error": "internal_error", "message": f"{type(e).__name__}: {e}"[:200]}

    def whoami(self, key: str, args: dict | None = None) -> dict:
        try:
            conn = self.store.connection(key)
            if conn is None:
                raise GraphError("reconnect_needed", "unknown or removed connection")
            out = {"account": conn["account"], "tenant": conn["tenant"], "connected_at": conn["connected_at"]}
            # Ask Microsoft too: a sign-in it now refuses is reconnect_needed here, not only at the next poll.
            if self._is_org(key):
                self._graph(key).get(f"/groups/{self.cfg.org_group}", {"$select": "id"})
                return {**out, "mailboxes": len(self.directory(key).members())}
            self._graph(key).get("/me", {"$select": "id"})
            return out
        except Exception as e:
            return self._failure(e)

    def list_folders(self, key: str, args: dict | None = None) -> dict:
        try:
            graph, out, root = self._graph(key), [], self._root(key, None)
            if self._is_org(key):
                want = str((args or {}).get("mailbox") or "").strip().lower()
                box = next((m for m in self.directory(key).members() if want in (m.id.lower(), m.mail.lower())), None) \
                    if want else None
                if box is None:
                    return {"error": "invalid_argument",
                            "message": "mailbox (the address or id of one of the group's mailboxes) is required"}
                root = self._root(key, box.id)
            for f in graph.get(f"{root}/mailFolders", {"$top": "100"}).get("value", []):
                out.append({"id": f["id"], "name": f["displayName"], "path": f["displayName"]})
                for c in graph.get(f"{root}/mailFolders/{f['id']}/childFolders", {"$top": "100"}).get("value", []):
                    out.append({"id": c["id"], "name": c["displayName"], "path": f"{f['displayName']}/{c['displayName']}"})
            return {"folders": out}                       # an object: MCP structured results are objects
        except Exception as e:
            return self._failure(e)

    def move_mail(self, key: str, args: dict) -> dict:
        """File an email into a folder (created, with its parents, when missing). The id stays the same."""
        mid, folder = str(args.get("mail_id") or "").strip(), str(args.get("folder") or "").strip()
        if not mid or not folder:
            return {"error": "invalid_argument", "message": "mail_id and folder are required"}
        try:
            box, gid = self._split(key, mid)
            graph, created, root = self._graph(key), [], self._root(key, box)
            dest = self._folder_id(graph, folder, created, root)
            graph.post(f"{root}/messages/{quote(gid, safe='')}/move", {"destinationId": dest})
            return {"id": mid, "folder": folder, "created": created}
        except Exception as e:
            return self._failure(e)

    def forward_mail(self, key: str, args: dict) -> dict:
        """Forward an email, with an optional note above it, to one or more addresses."""
        mid = str(args.get("mail_id") or "").strip()
        to = args.get("to") or []
        to = [str(a).strip() for a in (to.replace(";", ",").split(",") if isinstance(to, str) else to) if str(a).strip()]
        bad = [a for a in to if not EMAIL.fullmatch(a)]
        if not mid or not to or bad:
            return {"error": "invalid_argument",
                    "message": f"not an email address: {', '.join(bad)}" if bad else "mail_id and to are required"}
        try:
            box, gid = self._split(key, mid)
            self._graph(key).post(f"{self._root(key, box)}/messages/{quote(gid, safe='')}/forward", {
                "comment": str(args.get("comment") or ""),
                "toRecipients": [{"emailAddress": {"address": a}} for a in to]})
            return {"id": mid, "forwarded_to": to}
        except Exception as e:
            return self._failure(e)

    def get_attachment(self, key: str, args: dict) -> dict:
        """The ONLY place attachment content is downloaded or parsed (spec §5.1)."""
        aid = quote(str(args.get("attachment_id") or ""), safe="")
        try:
            box, gid = self._split(key, str(args.get("mail_id") or ""))
            graph, mid, root = self._graph(key), quote(gid, safe=""), self._root(key, box)
            meta = graph.get(f"{root}/messages/{mid}/attachments/{aid}", {"$select": "id,name,contentType,size"})
            name, ctype, size = meta.get("name", ""), meta.get("contentType", ""), int(meta.get("size") or 0)
            if meta.get("@odata.type") == REFERENCE_ATTACHMENT:
                return {"error": "cloud_attachment", "message": f"{name} is a cloud link, not a stored file: nothing to download"}
            if args.get("raw") and meta.get("@odata.type") == ITEM_ATTACHMENT:      # an attached email: its MIME, named <subject>.eml
                name = name or "Attached email"
                name, ctype = name if name.lower().endswith(".eml") else name + ".eml", "message/rfc822"
            if size > self.cfg.attachment_max_bytes:        # judged from metadata: nothing is downloaded
                return {"error": "too_large", "message": f"{name} is {size} bytes (limit {self.cfg.attachment_max_bytes})"}
            data = graph.get_bytes(f"{root}/messages/{mid}/attachments/{aid}/$value")
        except Exception as e:
            return self._failure(e)
        if len(data) > self.cfg.attachment_max_bytes:       # metadata size 0 or missing must not bypass the cap
            return {"error": "too_large", "message": f"{name} is {len(data)} bytes (limit {self.cfg.attachment_max_bytes})"}
        size = size or len(data)
        if args.get("raw"):
            return {"name": name, "content_type": ctype, "size": size, "base64": base64.b64encode(data).decode()}
        try:
            text = attachment_text(name, ctype, data)
        except Exception as e:  # noqa: BLE001 -- a corrupt/encrypted file is reported, never raised
            return {"error": "unreadable", "name": name, "content_type": ctype, "size": size,
                    "message": f"{type(e).__name__}: {e}"[:200]}
        if text is None:
            return {"name": name, "content_type": ctype, "size": size, "base64": base64.b64encode(data).decode()}
        return {"name": name, "content_type": ctype, "size": size, "text": text}

    def mail_to_pdf(self, key: str, args: dict) -> dict:
        """The email as one PDF (header, body, inline images). Attachments are not merged: get_attachment raw has them."""
        try:
            box, gid = self._split(key, str(args.get("mail_id") or ""))
            graph, mid, root = self._graph(key), quote(gid, safe=""), self._root(key, box)
            # contentId lives only on fileAttachment: the base-type $select must not name it (Graph answers 400)
            m = graph.get(f"{root}/messages/{mid}", {"$select": "from,toRecipients,ccRecipients,subject,receivedDateTime,body",
                                                  "$expand": ATTACHMENT_META})
            inline = {}
            for a in m.get("attachments") or []:
                if a.get("isInline") and a.get("@odata.type") not in ATTACHMENT_KINDS \
                        and str(a.get("contentType") or "").lower().startswith("image/") \
                        and int(a.get("size") or 0) <= self.cfg.attachment_max_bytes:
                    full = graph.get(f"{root}/messages/{mid}/attachments/{quote(a['id'], safe='')}")   # has contentId + contentBytes
                    if full.get("contentId") and full.get("contentBytes"):
                        data = base64.b64decode(full["contentBytes"])
                        if len(data) <= self.cfg.attachment_max_bytes:
                            inline[full["contentId"]] = data
        except Exception as e:
            return self._failure(e)
        try:
            data = render_mail(m, inline)
        except Exception as e:  # noqa: BLE001 -- even the text fallback failed
            return {"error": "render_failed", "message": f"{type(e).__name__}: {e}"[:200]}
        return {"name": "Email.pdf", "size": len(data), "base64": base64.b64encode(data).decode()}
