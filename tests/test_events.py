import asyncio
import dataclasses
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from events import NotificationRouter, Waiter, Wakes
from server import create_app
from subscriptions import IDLE_S, MAIL_MAX_MINUTES, RENEW_BEFORE_S, TICK_S, SubscriptionKeeper, epoch, mail_resource
from tests.test_server import call, connect

HUB = "EventHub:https://ns.servicebus.windows.net/eventhubname/hub?tenantId=contoso.onmicrosoft.com"
INBOX = mail_resource("inbox-id")


@pytest.fixture
def ecfg(cfg):
    return dataclasses.replace(cfg, notification_url=HUB)


class Clock:
    def __init__(self, t=time.time()):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def parts(fake, svc, ecfg):
    """Keeper + router + wakes over the svc fixture's store and connection "key-1"."""
    clock = Clock()
    svc.store._clock = fake.clock = clock                    # one clock for the store, the keeper and Graph
    keeper = SubscriptionKeeper(svc.store, svc._graph, ecfg, clock=clock)
    wakes = Wakes()
    router = NotificationRouter(svc.store, keeper, wakes)
    fake.on_notification = router.handle
    return keeper, router, wakes, clock


def subscribe_posts(fake):
    return [b for p, b in fake.posted if p.strip("/") == "subscriptions"]


# keeper ------------------------------------------------------------------------------------------
def test_first_ensure_subscribes_then_reuses(fake, parts):
    keeper, _, _, clock = parts
    assert keeper.ensure("key-1", INBOX) == {"events": "on"}
    assert keeper.ensure("key-1", INBOX) == {"events": "on"}
    [body] = subscribe_posts(fake)
    assert body["resource"] == INBOX and body["changeType"] == "created"
    assert body["notificationUrl"] == HUB and body["lifecycleNotificationUrl"] == HUB
    assert len(body["clientState"]) >= 32
    assert epoch(body["expirationDateTime"]) <= clock() + MAIL_MAX_MINUTES * 60
    sub = keeper.store.subscription("key-1", INBOX)
    assert sub["sub_id"] in fake.subscriptions and sub["client_state"] == body["clientState"]


def test_events_off_without_a_notification_url(fake, svc, cfg):
    keeper = SubscriptionKeeper(svc.store, svc._graph, cfg)
    assert keeper.ensure("key-1", INBOX) == {"events": "off", "reason": "EVENTHUB_NOTIFICATION_URL is not set"}
    assert subscribe_posts(fake) == []


def test_a_refused_subscription_is_not_retried_on_every_wait(fake, parts):
    keeper, _, _, clock = parts
    fake.subscription_status = 403
    off = keeper.ensure("key-1", INBOX)
    assert off["events"] == "off" and off["reason"].startswith("forbidden")
    assert keeper.ensure("key-1", INBOX)["events"] == "off"
    assert len(subscribe_posts(fake)) == 1
    fake.subscription_status = None
    clock.t += TICK_S + 1
    keeper.tick()                                            # the keeper retries
    assert keeper.ensure("key-1", INBOX) == {"events": "on"}
    assert len(subscribe_posts(fake)) == 2


def test_tick_renews_close_to_expiry(fake, parts):
    keeper, _, _, clock = parts
    keeper.ensure("key-1", INBOX)
    before = keeper.store.subscription("key-1", INBOX)["expires_at"]
    clock.t += before - clock.t - RENEW_BEFORE_S + 60       # a day minus a minute before expiry
    keeper.store.touch_subscription("key-1", INBOX)          # still waited on
    keeper.tick()
    assert keeper.store.subscription("key-1", INBOX)["expires_at"] > before
    assert ("PATCH", f"/v1.0/subscriptions/{keeper.store.subscription('key-1', INBOX)['sub_id']}") in fake.requests


def test_tick_recreates_a_subscription_graph_forgot(fake, parts):
    keeper, _, _, clock = parts
    keeper.ensure("key-1", INBOX)
    old = keeper.store.subscription("key-1", INBOX)
    fake.subscriptions.clear()
    clock.t = old["expires_at"] - 60
    keeper.store.touch_subscription("key-1", INBOX)
    keeper.tick()                                            # PATCH -> 404 -> POST
    new = keeper.store.subscription("key-1", INBOX)
    assert new["sub_id"] != old["sub_id"] and new["sub_id"] in fake.subscriptions


def test_tick_drops_a_watch_nobody_waits_on(fake, parts):
    keeper, _, _, clock = parts
    keeper.ensure("key-1", INBOX)
    sid = keeper.store.subscription("key-1", INBOX)["sub_id"]
    clock.t += IDLE_S + 1
    keeper.tick()
    assert keeper.store.subscription("key-1", INBOX) is None and sid not in fake.subscriptions


# router ------------------------------------------------------------------------------------------
def test_new_mail_wakes_the_folder(fake, parts):
    keeper, router, wakes, _ = parts
    keeper.ensure("key-1", INBOX)
    fake.add_message(subject="x", received="2026-10-07T10:01:00Z")
    fake.add_message(subject="other folder", folder="projects-id", received="2026-10-07T10:01:00Z")
    kid = keeper.store.key_id("key-1")
    assert wakes._take((kid, INBOX)) and not wakes._take((kid, INBOX))
    assert router.counts["woke"] == 1


def test_untrusted_and_validation_messages_wake_nothing(parts):
    keeper, router, wakes, _ = parts
    keeper.ensure("key-1", INBOX)
    sid = keeper.store.subscription("key-1", INBOX)["sub_id"]
    router.handle({"value": [{"subscriptionId": sid, "clientState": "forged", "changeType": "created"}]})
    router.handle({"value": [{"changeType": "Validation: Testing client application reachability for subscription",
                              "resource": "NA"}]})
    router.handle({"value": "nonsense"})
    router.handle([1, 2])
    assert router.counts == {"woke": 0, "ignored": 1, "rejected": 1, "lifecycle": 0}
    assert not wakes._take((keeper.store.key_id("key-1"), INBOX))


def test_lifecycle_events(fake, parts):
    keeper, router, wakes, _ = parts
    keeper.ensure("key-1", INBOX)
    key = (keeper.store.key_id("key-1"), INBOX)
    sid = keeper.store.subscription("key-1", INBOX)["sub_id"]
    fake.lifecycle(sid, "reauthorizationRequired")
    assert not wakes._take(key) and ("PATCH", f"/v1.0/subscriptions/{sid}") in fake.requests
    fake.lifecycle(sid, "missed")
    assert wakes._take(key)
    fake.lifecycle(sid, "subscriptionRemoved", remove=True)
    assert wakes._take(key)                                  # catch up on what came meanwhile
    new = keeper.store.subscription("key-1", INBOX)["sub_id"]
    assert new != sid and new in fake.subscriptions


# waiter ------------------------------------------------------------------------------------------
def test_wake_set_before_the_wait_is_kept():
    wakes = Wakes()
    wakes.set(("k", "r"))
    assert asyncio.run(wakes.wait(("k", "r"), 0.01)) is True
    assert asyncio.run(wakes.wait(("k", "r"), 0.01)) is False


def test_timeout_is_capped(svc, parts, ecfg):
    keeper, _, wakes, clock = parts
    waiter = Waiter(svc, keeper, wakes, svc.store, ecfg, clock)
    asked = []

    async def fake_wait(key, timeout):
        asked.append(timeout)
        return False

    wakes.wait = fake_wait
    prep = waiter.prepare("key-1", {"args": {"folder": "Inbox"}})
    assert asyncio.run(waiter.wait(prep, None))["via"] == "fallback"      # first wait after start
    asyncio.run(waiter.wait(prep, 1000))
    asyncio.run(waiter.wait(prep, None))
    assert asked == [150, 120]
    assert asyncio.run(waiter.wait(prep, "soon"))["error"] == "invalid_argument"


def test_filter_sets_on_one_folder_share_a_subscription(fake, svc, parts, ecfg):
    keeper, _, wakes, clock = parts
    waiter = Waiter(svc, keeper, wakes, svc.store, ecfg, clock)
    a = waiter.prepare("key-1", {"args": {"folder": "Inbox"}})
    b = waiter.prepare("key-1", {"args": {"folder": "Inbox", "subject_contains": "invoice"}})
    assert a["key"] == b["key"] and a["scope"] != b["scope"]
    assert len(subscribe_posts(fake)) == 1


# over MCP ----------------------------------------------------------------------------------------
def wait(c, key, timeout=5, **args):
    return call(c, "wait_for_new", {"args": {"folder": "Inbox", **args}, "timeout_s": timeout}, key=key)


def test_wait_over_mcp(fake, http, ecfg):
    c = TestClient(create_app(ecfg, http=http, fake=fake))
    key = connect(c, fake)
    assert call(c, "wait_for_new", {}) ["error"] == "not_connected"
    assert call(c, "wait_for_new", {"poll": "poll_new_file"}, key=key)["error"] == "invalid_argument"
    assert wait(c, key) == {"status": "ready", "via": "fallback", "events": "on"}   # first wait after start
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key) == {"mail": None}
    t0 = time.monotonic()
    assert wait(c, key, timeout=0.3) == {"status": "timeout", "events": "on"}
    assert time.monotonic() - t0 >= 0.3

    threading.Timer(0.3, lambda: fake.add_message(subject="Hello", received="2099-01-01T00:00:00Z")).start()
    t0 = time.monotonic()
    assert wait(c, key, timeout=10) == {"status": "ready", "via": "event", "events": "on"}
    assert time.monotonic() - t0 < 5
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]["subject"] == "Hello"


def test_queued_mail_is_ready_at_once(fake, http, ecfg):
    c = TestClient(create_app(ecfg, http=http, fake=fake))
    key = connect(c, fake)
    wait(c, key)
    fake.on_notification = None                              # no event: only the queue can tell
    fake.add_message(subject="a", received="2099-01-01T00:00:00Z")
    fake.add_message(subject="b", received="2099-01-01T00:01:00Z")
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]["subject"] == "a"
    assert wait(c, key)["via"] == "pending"                  # "b" is queued, undelivered


def test_events_off_still_polls_on_the_fallback(fake, http, cfg):
    c = TestClient(create_app(dataclasses.replace(cfg, watch_fallback_s=1), http=http, fake=fake))
    key = connect(c, fake)
    assert wait(c, key)["via"] == "fallback"
    t0 = time.monotonic()
    r = wait(c, key, timeout=10)
    assert r == {"status": "ready", "via": "fallback", "events": "off", "reason": "EVENTHUB_NOTIFICATION_URL is not set"}
    assert 0.5 < time.monotonic() - t0 < 5


def test_waiters_hold_no_worker_thread(fake, http, ecfg):
    c = TestClient(create_app(ecfg, http=http, fake=fake))
    key = connect(c, fake)
    wait(c, key)
    threads = [threading.Thread(target=wait, args=(c, key, 3)) for _ in range(60)]   # > the 40-thread pool
    for t in threads:
        t.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    assert call(c, "whoami", key=key)["account"] == "me@contoso.com"
    assert time.monotonic() - t0 < 2
    for t in threads:
        t.join()
