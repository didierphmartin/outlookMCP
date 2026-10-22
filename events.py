"""Change notifications in, wakes out. A notification never carries mail: it only wakes the waiters of a
folder, and the next poll_new_mail (delta + queue) finds what is new. Lost or duplicate notifications are
harmless for the same reason."""
from __future__ import annotations

import asyncio
import json
import threading


class Wakes:
    """One wake flag per watched resource, (key_id, resource). Set from any thread (the hub listener);
    awaited on an event loop. A wake with nobody waiting is kept until the next wait takes it."""

    def __init__(self):
        self._pending: set[tuple[str, str]] = set()
        self._waiters: dict[tuple[str, str], set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
        self._lock = threading.Lock()

    def set(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._pending.add(key)
            waiters = list(self._waiters.get(key, ()))
        for loop, ev in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(ev.set)

    def _take(self, key: tuple[str, str]) -> bool:
        with self._lock:
            if key in self._pending:
                self._pending.discard(key)
                return True
            return False

    async def wait(self, key: tuple[str, str], timeout: float) -> bool:
        """True when woken (now or earlier), False after timeout seconds."""
        me = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            if key in self._pending:
                self._pending.discard(key)
                return True
            self._waiters.setdefault(key, set()).add(me)
        try:
            await asyncio.wait_for(me[1].wait(), max(timeout, 0))
            woke = True
        except asyncio.TimeoutError:
            woke = False
        finally:
            with self._lock:
                self._waiters[key].discard(me)
                if not self._waiters[key]:
                    del self._waiters[key]
        return self._take(key) or woke


class NotificationRouter:
    """Graph's notification bodies ({"value": [...]}) -> wakes, plus the keeper's lifecycle duties."""

    def __init__(self, store, keeper, wakes: Wakes):
        self.store, self.keeper, self.wakes = store, keeper, wakes
        self.counts = {"woke": 0, "ignored": 0, "rejected": 0, "lifecycle": 0}

    def handle(self, body: dict) -> None:
        items = body.get("value") if isinstance(body, dict) else None
        for n in items if isinstance(items, list) else []:
            self._one(n if isinstance(n, dict) else {})

    def _one(self, n: dict) -> None:
        # Graph also drops "Validation: Testing client application reachability ..." messages into the hub
        # when a subscription is created: they name no subscription.
        sub = self.store.subscription_by_id(n.get("subscriptionId") or "") if n.get("subscriptionId") else None
        if sub is None:
            self.counts["ignored"] += 1
            return
        if not sub["client_state"] or n.get("clientState") != sub["client_state"]:
            self.counts["rejected"] += 1                  # not from our subscription: never trusted
            return
        match n.get("lifecycleEvent"):
            case "reauthorizationRequired":
                self.counts["lifecycle"] += 1
                self.keeper.renew(sub)
                return
            case "subscriptionRemoved":
                self.counts["lifecycle"] += 1
                self.keeper.recreate(sub)                 # then catch up on what came meanwhile
            case "missed":
                self.counts["lifecycle"] += 1
            case _:
                pass
        self.counts["woke"] += 1
        self.wakes.set((sub["key_id"], sub["resource"]))


class EventHubListener:  # pragma: no cover -- needs a real hub; the router it feeds is tested
    """Reads the hub in a background thread from where the last run stopped (positions in the store)."""

    def __init__(self, cfg, store, router: NotificationRouter):
        self.cfg, self.store, self.router = cfg, store, router
        self.client = None
        self.error = ""

    def start(self) -> None:
        """A bad connection string never stops the server: events are off and wait_for_new says why."""
        try:
            from azure.eventhub import EventHubConsumerClient
            self.client = EventHubConsumerClient.from_connection_string(
                self.cfg.eventhub_connection, consumer_group=self.cfg.eventhub_consumer_group)
        except Exception as e:  # noqa: BLE001
            self.error = f"EVENTHUB_CONNECTION: {e}"[:300]
            return
        positions = self.store.hub_positions()
        threading.Thread(target=self._receive, args=(positions,), name="eventhub-listener", daemon=True).start()

    def _receive(self, positions: dict) -> None:
        try:
            self.client.receive(on_event=self._on_event, on_error=self._on_error,
                                starting_position=positions or "@latest", starting_position_inclusive=False)
        except Exception as e:  # noqa: BLE001 -- reported by wait_for_new; timed polling keeps working
            self.error = f"{type(e).__name__}: {e}"[:300]

    def _on_event(self, partition_context, event) -> None:
        if event is None:
            return
        try:
            self.router.handle(json.loads(event.body_as_str()))
        except (ValueError, TypeError):
            pass                                          # not a Graph notification: skipped, not retried
        self.store.save_hub_position(partition_context.partition_id, event.sequence_number)

    def _on_error(self, partition_context, error) -> None:
        self.error = f"{type(error).__name__}: {error}"[:300]

    def stop(self) -> None:
        if self.client is not None:
            self.client.close()


POLL_ARGS = ("folder", "mode", "from", "subject_contains", "has_attachments")   # accepted flat by wait_for_new
MAX_WAIT_S = 150          # under the 180 s tool timeout of gpt's compiled scripts
DEFAULT_WAIT_S = 120


class Waiter:
    """wait_for_new: answers ready when a notification arrived for the poll's folder, when undelivered mail
    is already queued (in drop mode: or a delivered one's lease has run out), or when the safety poll is due; timeout otherwise. Never returns mail itself."""

    def __init__(self, mail, keeper, wakes: Wakes, store, cfg, clock):
        self.mail, self.keeper, self.wakes, self.store, self.cfg, self.clock = mail, keeper, wakes, store, cfg, clock
        self._last_ready: dict[tuple[str, str], float] = {}   # (key_id, scope) -> when it was last ready
        self.listener_error = lambda: ""

    def prepare(self, connection_key: str, args: dict) -> dict:
        """The blocking part (Graph calls), run on a worker thread: the folder, its subscription, the queue."""
        poll = args.get("poll") or "poll_new_mail"
        if poll != "poll_new_mail":
            return {"error": "invalid_argument", "message": f"poll must be poll_new_mail, not {poll!r}"}
        nested = args.get("args") or {}
        if not isinstance(nested, dict):
            return {"error": "invalid_argument", "message": "args must be an object: poll_new_mail's arguments"}
        # The poll's arguments may also come flat, at the top level; the nested ones win.
        poll_args = {**{k: args[k] for k in POLL_ARGS if k in args}, **nested}
        try:
            scope, folder_id, mode = self.mail.watch_scope(connection_key, poll_args)
        except Exception as e:  # noqa: BLE001 -- the tool never raises
            return self.mail._failure(e)
        from subscriptions import mail_resource
        resource = mail_resource(folder_id)
        events = self.keeper.ensure(connection_key, resource)
        if events["events"] == "on" and self.listener_error():
            events = {"events": "off", "reason": "event hub: " + self.listener_error()}
        kid = self.store.key_id(connection_key)
        return {"key": (kid, resource), "scope": (kid, scope), "events": events,
                "queued": self.store.has_undelivered(connection_key, scope, self.mail.lease_s(mode))}

    async def wait(self, prep: dict, timeout_s) -> dict:
        try:
            timeout = min(max(float(timeout_s if timeout_s is not None else DEFAULT_WAIT_S), 0), MAX_WAIT_S)
        except (TypeError, ValueError):
            return {"error": "invalid_argument", "message": "timeout_s must be a number of seconds"}
        now = self.clock()
        # A scope first seen since the server started is ready at once: mail may have come while it was down.
        last = self._last_ready.get(prep["scope"], now - self.cfg.watch_fallback_s)
        due = last + self.cfg.watch_fallback_s
        if prep["queued"] or self.wakes._take(prep["key"]):
            return self._ready(prep, "pending")
        if due <= now:
            return self._ready(prep, "fallback")
        if await self.wakes.wait(prep["key"], min(timeout, due - now)):
            return self._ready(prep, "event")
        if self.clock() >= due:
            return self._ready(prep, "fallback")
        return {"status": "timeout", **prep["events"]}

    def _ready(self, prep: dict, via: str) -> dict:
        self._last_ready[prep["scope"]] = self.clock()
        return {"status": "ready", "via": via, **prep["events"]}
