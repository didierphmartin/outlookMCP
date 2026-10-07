"""The mailbox tools. poll_new_mail never touches attachment content (spec §5.1)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from auth import AuthError
from extract import body_text
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
                   bool(args.get("has_attachments")))

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

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _graph(self, key: str):
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
        if cur is None:
            since = self.now_iso()
            url, params = f"/me/mailFolders/{folder_id}/messages/delta", {
                "$filter": f"receivedDateTime ge {since}", "$select": DELTA_SELECT}
        else:
            since, url, params = cur["since"], cur["delta_link"], None
        found = []
        while True:
            page = graph.get(url, params)
            params = None
            for m in page.get("value", []):
                if "@removed" not in m and (m.get("receivedDateTime") or "") >= since and filters.match(m):
                    found.append((m["id"], m["receivedDateTime"]))
            if "@odata.nextLink" in page:
                url = page["@odata.nextLink"]
                continue
            delta_link = page["@odata.deltaLink"]
            break
        self.store.enqueue(key, scope, found)       # before the cursor: a crash re-reads, never loses
        self.store.save_cursor(key, scope, delta_link, since)

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
                        case _:
                            self.store.release(key, scope, mid)   # never handed out: offer it next poll
                            raise
                return {"mail": Message.shape(m)}
            return {"mail": None}
        except GraphError as e:
            return {"error": e.code, "message": e.message}
        except AuthError as e:
            return {"error": "reconnect_needed", "message": str(e)}
        except httpx.HTTPError as e:
            return {"error": "network_error", "message": f"{type(e).__name__}: {e}"[:200]}
        except ValueError as e:                                     # a non-JSON answer from Graph
            return {"error": "graph_error", "message": str(e)[:200]}
