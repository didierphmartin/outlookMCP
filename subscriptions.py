"""Graph change subscriptions for watched folders: created by the first wait, renewed while waited on,
dropped when nobody has waited for a day. One subscription per (connection, folder), shared by every
filter set on that folder."""
from __future__ import annotations

import re
import secrets
import threading
import time
from datetime import datetime, timezone

from graph import GraphError

MAIL_MAX_MINUTES = 10070       # Graph refuses more for messages ("can only be 10070 minutes in the future")
RENEW_BEFORE_S = 24 * 3600     # renew a subscription that expires within a day
IDLE_S = 24 * 3600             # drop a subscription nobody has waited on for a day
TICK_S = 15 * 60


def mail_resource(folder_id: str, mailbox_id: str | None = None) -> str:
    """The signed-in user's folder (me/...), or a mailbox of an organization connection (users/{id}/...)."""
    root = "me" if mailbox_id is None else f"users/{mailbox_id}"
    return f"{root}/mailFolders('{folder_id}')/messages"


def folder_of(resource: str) -> str | None:
    """The folder id of a mail_resource (either form), None for any other resource."""
    m = re.fullmatch(r"(?:me|users/[^/]+)/mailFolders\('(.+)'\)/messages", resource or "")
    return m.group(1) if m else None


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(iso_text: str) -> float:
    return datetime.fromisoformat(iso_text.replace("Z", "+00:00")).timestamp()


class SubscriptionKeeper:
    def __init__(self, store, graph_for, cfg, clock=time.time):
        self.store, self.graph_for, self.cfg, self.clock = store, graph_for, cfg, clock
        self._lock = threading.Lock()                  # one create per resource, even with concurrent waits

    def ensure(self, connection_key: str, resource: str) -> dict:
        """Make sure Graph notifies this resource. {"events": "on"} or {"events": "off", "reason": ...};
        an "off" watch still works by timed polling."""
        if not self.cfg.events_enabled:
            return {"events": "off", "reason": "EVENTHUB_NOTIFICATION_URL is not set"}
        with self._lock:
            sub = self.store.subscription(connection_key, resource)
            if sub and sub["sub_id"] and sub["expires_at"] > self.clock() + 60:
                self.store.touch_subscription(connection_key, resource)
                return {"events": "on"}
            if sub and not sub["sub_id"] and sub["expires_at"] > self.clock():
                # A recent create failed: no retry on every wait; the keeper retries (expires_at = retry time).
                self.store.touch_subscription(connection_key, resource)
                return {"events": "off", "reason": sub["error"]}
            sub = self._create(connection_key, resource)
        return {"events": "on"} if sub["sub_id"] else {"events": "off", "reason": sub["error"]}

    def _create(self, connection_key: str, resource: str) -> dict:
        client_state = secrets.token_urlsafe(32)
        body = {"changeType": "created", "resource": resource, "notificationUrl": self._url(),
                "lifecycleNotificationUrl": self._url(), "clientState": client_state,
                "expirationDateTime": iso(self.clock() + (MAIL_MAX_MINUTES - 10) * 60)}
        try:
            r = self.graph_for(connection_key).post("/subscriptions", body)
            self.store.save_subscription(connection_key, resource, r["id"], client_state, epoch(r["expirationDateTime"]))
        except Exception as e:  # noqa: BLE001 -- recorded; the watch falls back to timed polling
            self.store.save_subscription(connection_key, resource, None, "", self.clock() + TICK_S, error=_reason(e))
        return self.store.subscription(connection_key, resource)

    def _url(self) -> str:
        return self.cfg.notification_url or "EventHub:fake"

    def renew(self, sub: dict) -> None:
        """Push the expiry out again (also what Graph's reauthorizationRequired asks for). A subscription Graph
        no longer knows is created again."""
        try:
            r = self.graph_for(sub["connection_key"]).patch(
                f"/subscriptions/{sub['sub_id']}", {"expirationDateTime": iso(self.clock() + (MAIL_MAX_MINUTES - 10) * 60)})
            self.store.save_subscription(sub["connection_key"], sub["resource"], sub["sub_id"], sub["client_state"],
                                         epoch(r["expirationDateTime"]))
        except GraphError as e:
            if e.code != "not_found":
                self.store.save_subscription(sub["connection_key"], sub["resource"], sub["sub_id"], sub["client_state"],
                                             sub["expires_at"], error=_reason(e))
                return
            self.recreate(sub)

    def recreate(self, sub: dict) -> None:
        with self._lock:
            self._create(sub["connection_key"], sub["resource"])

    def drop(self, sub: dict) -> None:
        if sub["sub_id"]:
            try:
                self.graph_for(sub["connection_key"]).delete(f"/subscriptions/{sub['sub_id']}")
            except GraphError:
                pass                                       # gone already, or the sign-in is dead: it expires anyway
        self.store.delete_subscription(sub["connection_key"], sub["resource"])

    def drop_mailboxes(self, connection_key: str, mailbox_ids: list[str]) -> None:
        """The subscriptions of mailboxes that left an organization connection's group."""
        kid = self.store.key_id(connection_key)
        prefixes = tuple(f"users/{m}/" for m in mailbox_ids)
        for sub in self.store.subscriptions():
            if sub["key_id"] == kid and sub["resource"].startswith(prefixes):
                self.drop(sub)

    def tick(self) -> None:
        """Every TICK_S: drop idle watches, renew the ones close to expiry, retry the ones that failed."""
        now = self.clock()
        for sub in self.store.subscriptions():
            if sub["last_wait_at"] < now - IDLE_S:
                self.drop(sub)
            elif not sub["sub_id"]:
                self.recreate(sub)
            elif sub["expires_at"] < now + RENEW_BEFORE_S:
                self.renew(sub)

    def run(self, stop: threading.Event) -> None:  # pragma: no cover -- the thread around tick()
        while not stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- one bad pass never stops the keeper
                pass


def _reason(e: Exception) -> str:
    match e:
        case GraphError():
            return f"{e.code}: {e.message}"[:300]
        case _:
            return f"{type(e).__name__}: {e}"[:300]
