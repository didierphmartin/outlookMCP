"""The mailboxes of an organization connection: the members of one Entra group, re-read every refresh_s."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

UNSAFE = re.compile(r'[/\\:*?"<>|]')          # characters SharePoint rejects in a folder name


@dataclass(frozen=True)
class Mailbox:
    id: str            # the Entra user id: /users/{id}/...
    mail: str          # the address
    name: str          # a safe, unique folder name (see MailboxDirectory.safe_name)


class MailboxDirectory:
    def __init__(self, graph, group: str, clock=time.time, refresh_s: int = 900):
        self.graph, self.group, self.clock, self.refresh_s = graph, group, clock, refresh_s
        self._members: list[Mailbox] | None = None
        self._read_at = 0.0
        self._lock = threading.Lock()
        self._listeners = []

    @staticmethod
    def safe_name(display_name: str, mail: str = "") -> str:
        """/ \\ : * ? " < > | become -, leading/trailing dots and spaces go; nothing left -> the address."""
        return UNSAFE.sub("-", display_name or "").strip(". ") or mail

    def on_departed(self, listener) -> None:
        """listener(list[Mailbox]) is called when a re-read finds members gone since the previous read."""
        self._listeners.append(listener)

    def members(self) -> list[Mailbox]:
        with self._lock:
            if self._members is None or self.clock() - self._read_at >= self.refresh_s:
                fresh = self._read()
                gone = [m for m in self._members or [] if m.id not in {f.id for f in fresh}]
                self._members, self._read_at = fresh, self.clock()
            else:
                gone = []
            members = list(self._members)
        if gone:
            for listener in self._listeners:
                listener(gone)
        return members

    def _read(self) -> list[Mailbox]:
        users, url = [], f"/groups/{self.group}/members/microsoft.graph.user"
        params = {"$select": "id,mail,displayName"}
        while url:
            page = self.graph.get(url, params)
            users += [u for u in page.get("value", []) if u.get("mail")]      # no mail: no mailbox
            url, params = page.get("@odata.nextLink"), None
        names = [self.safe_name(u.get("displayName") or "", u["mail"]) for u in users]
        folded = [n.casefold() for n in names]                             # SharePoint names ignore case
        return [Mailbox(u["id"], u["mail"], n if folded.count(n.casefold()) == 1 else self.safe_name(f"{n} ({u['mail']})"))
                for u, n in zip(users, names)]
