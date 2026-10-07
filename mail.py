"""The mailbox tools. poll_new_mail never touches attachment content (spec §5.1)."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from cryptography.fernet import InvalidToken

from auth import AuthError
from extract import attachment_text, body_text
from graph import GraphError

DELTA_SELECT = "id,from,subject,receivedDateTime,hasAttachments"
FULL_SELECT = "id,from,toRecipients,ccRecipients,subject,receivedDateTime,body,webLink,hasAttachments"
ATTACHMENT_META = "attachments($select=id,name,contentType,size)"   # metadata only, never contentBytes


class Message:
    """Graph message dicts -> the tool's mail shape."""

    @staticmethod
    def address(recipient: dict | None) -> str:
        return ((recipient or {}).get("emailAddress") or {}).get("address", "")

    @classmethod
    def shape(cls, m: dict) -> dict:
        return {"id": m["id"], "from": cls.address(m.get("from")),
                "to": [cls.address(r) for r in m.get("toRecipients") or []],
                "cc": [cls.address(r) for r in m.get("ccRecipients") or []],
                "subject": m.get("subject") or "", "received_at": m.get("receivedDateTime"),
                "body_text": body_text(m.get("body")), "web_link": m.get("webLink") or "",
                "attachments": [{"id": a["id"], "name": a.get("name", ""), "content_type": a.get("contentType", ""),
                                 "size": int(a.get("size") or 0)} for a in m.get("attachments") or []]}


@dataclass(frozen=True)
class Filters:
    sender: str = ""
    subject_contains: str = ""
    has_attachments: bool = False

    @classmethod
    def from_args(cls, args: dict) -> "Filters":
        return cls(str(args.get("from") or "").strip().lower(), str(args.get("subject_contains") or "").strip().lower(),
                   cls._flag(args.get("has_attachments")))

    @staticmethod
    def _flag(value) -> bool:
        match value:
            case str():
                return value.strip().lower() not in ("", "false", "0", "no", "off")
            case _:
                return bool(value)

    def signature(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]

    def match(self, m: dict) -> bool:
        return ((not self.sender or self.sender in Message.address(m.get("from")).lower())
                and (not self.subject_contains or self.subject_contains in (m.get("subject") or "").lower())
                and (not self.has_attachments or bool(m.get("hasAttachments"))))


class MailService:
    def __init__(self, graph_for, store, cfg, now_iso=None):
        self.graph_for, self.store, self.cfg = graph_for, store, cfg
        self.now_iso = now_iso or self.utc_now
        self._graphs: dict = {}          # connection key -> GraphClient (one TokenSource per connection)
        self._graphs_lock = threading.Lock()

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _graph(self, key: str):
        with self._graphs_lock:
            if key not in self._graphs:
                self._graphs[key] = self.graph_for(key)
            return self._graphs[key]

    # folders -------------------------------------------------------------------------
    def _folder_id(self, graph, folder: str) -> str:
        """'Inbox', a well-known name, an id, a top-level display name, or a path 'Parent/Child' -> the folder id."""
        parts = [p for p in (folder or "Inbox").split("/") if p] or ["Inbox"]
        try:
            current = graph.get(f"/me/mailFolders/{quote(parts[0].replace(' ', ''), safe='')}")["id"]
        except GraphError as e:
            match e.code:
                case "not_found" | "graph_error":   # 404, or 400 for a name that is not well-known: look it up by name
                    current = self._child(graph, None, parts[0])
                case _:
                    raise
        for name in parts[1:]:
            current = self._child(graph, current, name)
        return current

    @staticmethod
    def _child(graph, parent: str | None, name: str) -> str:
        path = f"/me/mailFolders/{parent}/childFolders" if parent else "/me/mailFolders"
        for f in graph.get(path, {"$top": "100"}).get("value", []):
            if f["displayName"].lower() == name.lower():
                return f["id"]
        raise GraphError("unknown_folder", name)

    # queue ---------------------------------------------------------------------------
    def _refresh_queue(self, graph, key: str, scope: str, folder_id: str, filters: Filters) -> None:
        """Read the folder's delta since the cursor and queue the matching new messages. The queue (not the
        delta order) decides delivery order; re-sent (changed) messages are already queued and stay ignored."""
        cur = self.store.cursor(key, scope)
        if cur is None:                             # first use: "now", saved before Graph is asked, so a failed
            cur = {"delta_link": None, "since": self.now_iso()}   # first round does not move it forward
            self.store.save_cursor(key, scope, None, cur["since"])
        since = cur["since"]
        fresh = (f"/me/mailFolders/{folder_id}/messages/delta",
                 {"$filter": f"receivedDateTime ge {since}", "$select": DELTA_SELECT})
        try:
            found, delta_link = self._read_delta(graph, *((cur["delta_link"], None) if cur["delta_link"] else fresh),
                                                 since, filters)
        except GraphError as e:
            if e.code != "sync_state_lost" or not cur["delta_link"]:
                raise
            # The delta link is stale (410 / SyncState*): start again from `since`. Already queued or delivered
            # messages come back too, and the queue ignores them, so nothing is delivered twice.
            found, delta_link = self._read_delta(graph, *fresh, since, filters)
        self.store.enqueue(key, scope, found)       # before the cursor: a crash re-reads, never loses
        self.store.save_cursor(key, scope, delta_link, since)

    @staticmethod
    def _read_delta(graph, url: str, params: dict | None, since: str, filters: Filters) -> tuple[list, str]:
        """Every page of one delta round -> ([(id, receivedDateTime) of matching new mail], the next delta link)."""
        found = []
        while True:
            page = graph.get(url, params)
            params = None
            for m in page.get("value", []):
                if "@removed" not in m and (m.get("receivedDateTime") or "") >= since and filters.match(m):
                    found.append((m["id"], m["receivedDateTime"]))
            if "@odata.nextLink" not in page:
                return found, page["@odata.deltaLink"]
            url = page["@odata.nextLink"]

    def _scope(self, graph, args: dict) -> tuple[str, str, Filters]:
        filters = Filters.from_args(args)
        folder_id = self._folder_id(graph, args.get("folder") or "Inbox")
        return f"{folder_id}|{filters.signature()}", folder_id, filters

    def _refresh_queue_for_tests(self, key: str, folder: str, args: dict) -> None:
        graph = self._graph(key)
        scope, folder_id, filters = self._scope(graph, {**args, "folder": folder})
        self._refresh_queue(graph, key, scope, folder_id, filters)

    # tools ---------------------------------------------------------------------------
    def poll_new_mail(self, key: str, args: dict) -> dict:
        try:
            graph = self._graph(key)
            scope, folder_id, filters = self._scope(graph, args)
            self._refresh_queue(graph, key, scope, folder_id, filters)
            while (mid := self.store.claim_next(key, scope)) is not None:
                try:
                    m = graph.get(f"/me/messages/{mid}", {"$select": FULL_SELECT, "$expand": ATTACHMENT_META})
                except Exception as e:
                    match e:
                        case GraphError(code="not_found"):
                            continue                              # deleted meanwhile: skip it
                        case GraphError() if e.transient or e.code == "reconnect_needed":
                            self.store.release(key, scope, mid)   # never handed out: offer it next poll
                        case httpx.HTTPError() | ValueError():    # the network, or a non-JSON gateway page
                            self.store.release(key, scope, mid)
                    raise                                         # permanent: the message counts as consumed
                return {"mail": Message.shape(m)}                 # a shape failure is permanent too: consumed
            return {"mail": None}
        except Exception as e:                                      # the tool never raises
            return self._failure(e)

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
            return {"account": conn["account"], "tenant": conn["tenant"], "connected_at": conn["connected_at"]}
        except Exception as e:
            return self._failure(e)

    def list_folders(self, key: str, args: dict | None = None):
        try:
            graph, out = self._graph(key), []
            for f in graph.get("/me/mailFolders", {"$top": "100"}).get("value", []):
                out.append({"id": f["id"], "name": f["displayName"], "path": f["displayName"]})
                for c in graph.get(f"/me/mailFolders/{f['id']}/childFolders", {"$top": "100"}).get("value", []):
                    out.append({"id": c["id"], "name": c["displayName"], "path": f"{f['displayName']}/{c['displayName']}"})
            return out
        except Exception as e:
            return self._failure(e)

    def get_attachment(self, key: str, args: dict) -> dict:
        """The ONLY place attachment content is downloaded or parsed (spec §5.1)."""
        mid, aid = quote(str(args.get("mail_id") or ""), safe=""), quote(str(args.get("attachment_id") or ""), safe="")
        try:
            graph = self._graph(key)
            meta = graph.get(f"/me/messages/{mid}/attachments/{aid}", {"$select": "id,name,contentType,size"})
            name, ctype, size = meta.get("name", ""), meta.get("contentType", ""), int(meta.get("size") or 0)
            if size > self.cfg.attachment_max_bytes:        # judged from metadata: nothing is downloaded
                return {"error": "too_large", "message": f"{name} is {size} bytes (limit {self.cfg.attachment_max_bytes})"}
            data = graph.get_bytes(f"/me/messages/{mid}/attachments/{aid}/$value")
        except Exception as e:
            return self._failure(e)
        if len(data) > self.cfg.attachment_max_bytes:       # metadata size 0 or missing must not bypass the cap
            return {"error": "too_large", "message": f"{name} is {len(data)} bytes (limit {self.cfg.attachment_max_bytes})"}
        size = size or len(data)
        try:
            text = attachment_text(name, ctype, data)
        except Exception as e:  # noqa: BLE001 -- a corrupt/encrypted file is reported, never raised
            return {"error": "unreadable", "name": name, "content_type": ctype, "size": size,
                    "message": f"{type(e).__name__}: {e}"[:200]}
        if text is None:
            return {"name": name, "content_type": ctype, "size": size, "base64": base64.b64encode(data).decode()}
        return {"name": name, "content_type": ctype, "size": size, "text": text}
