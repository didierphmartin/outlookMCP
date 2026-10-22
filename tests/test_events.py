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
    [prefer] = [h.get("prefer") for (m, p), h in zip(fake.requests, fake.headers) if m == "POST" and p.endswith("/subscriptions")]
    assert prefer == 'IdType="ImmutableId"'                  # notifications then name messages by their immutable id


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


# drop mode ---------------------------------------------------------------------------------------
def test_wait_flat_args_watch_drop_scope(fake, svc, parts, ecfg):
    keeper, _, wakes, clock = parts
    waiter = Waiter(svc, keeper, wakes, svc.store, ecfg, clock)
    flat = {"folder": "To SharePoint", "mode": "drop"}
    prep = waiter.prepare("key-1", flat)                     # the drop folder is created, not unknown_folder
    assert "error" not in prep and prep["queued"] is False
    assert prep["scope"] == waiter.prepare("key-1", {"args": flat})["scope"]
    assert prep["scope"] == waiter.prepare("key-1", {"folder": "Inbox", "args": flat})["scope"]   # nested wins
    svc.poll_new_mail("key-1", flat)                         # baseline
    fake.add_message(folder="To SharePoint", subject="x", received="2026-10-01T08:00:00Z")
    svc._refresh_queue_for_tests("key-1", "To SharePoint", {"mode": "drop"})
    prep = waiter.prepare("key-1", flat)
    assert prep["queued"] is True
    assert asyncio.run(waiter.wait(prep, 0))["via"] == "pending"


def test_wait_ready_when_lease_expired(fake, svc, parts, ecfg):
    keeper, _, wakes, clock = parts
    waiter = Waiter(svc, keeper, wakes, svc.store, ecfg, clock)
    flat = {"folder": "To SharePoint", "mode": "drop"}
    svc.poll_new_mail("key-1", flat)
    fake.add_message(folder="To SharePoint", subject="x", received="2026-10-01T08:00:00Z")
    assert svc.poll_new_mail("key-1", flat)["mail"]["attempt"] == 1
    assert waiter.prepare("key-1", flat)["queued"] is False
    clock.t += 600
    assert waiter.prepare("key-1", flat)["queued"] is True


# drop mode: a "created" notification is the email arriving in the folder now -------------------------
DROP = "To SharePoint"


def drop_poll(svc, folder=DROP):
    return svc.poll_new_mail("key-1", {"folder": folder, "mode": "drop"})


def watch_drop(svc, keeper, folder=DROP):
    """Baseline round (creates the folder) + the folder's subscription."""
    assert drop_poll(svc, folder) == {"mail": None}
    _, folder_id, _ = svc.watch_scope("key-1", {"folder": folder, "mode": "drop"})
    keeper.ensure("key-1", mail_resource(folder_id))
    return folder_id


def rows(svc, mid):
    return svc.store._x("SELECT scope, delivered, attempts, baseline, in_folder FROM queue WHERE message_id = ? "
                        "ORDER BY scope", (mid,))


def test_drop_created_notification_delivers_a_baseline_email(fake, svc, parts):
    keeper, router, wakes, _ = parts
    drop_poll(svc)                                           # creates the folder
    mid = fake.add_message(folder=DROP, subject="already there", received="2026-10-01T08:00:00Z")
    svc.store._x("DELETE FROM cursors")                      # a fresh watch: mid is its baseline
    folder_id = watch_drop(svc, keeper)
    assert drop_poll(svc) == {"mail": None}
    fake.notify_created(mid)                                 # dragged out and back between two delta rounds
    assert wakes._take((keeper.store.key_id("key-1"), mail_resource(folder_id)))
    mail = drop_poll(svc)["mail"]
    assert mail["id"] == mid and mail["attempt"] == 1


def test_drop_created_notification_retries_an_exhausted_email(fake, svc, parts):
    keeper, router, _, clock = parts
    watch_drop(svc, keeper)
    mid = fake.add_message(folder=DROP, subject="x", received="2026-10-01T08:00:00Z")
    for attempt in (1, 2, 3):
        assert drop_poll(svc)["mail"]["attempt"] == attempt
        clock.t += 600
    assert drop_poll(svc) == {"mail": None}                  # 3 attempts: left in the folder
    fake.notify_created(mid)                                 # the user drags it out and back
    mail = drop_poll(svc)["mail"]
    assert mail["id"] == mid and mail["attempt"] == 1


def test_drop_change_without_notification_redelivers_nothing(fake, svc, parts):
    keeper, _, _, _ = parts
    watch_drop(svc, keeper)
    mid = fake.add_message(folder=DROP, subject="x", received="2026-10-01T08:00:00Z")
    assert drop_poll(svc)["mail"]["attempt"] == 1
    fake.touch_message(mid)                                  # read / flagged: no "created" notification
    assert drop_poll(svc) == {"mail": None}


def test_drop_created_notification_for_an_unknown_message_adds_no_row(fake, svc, parts, caplog):
    caplog.set_level("INFO", logger="outlook_mcp.events")
    keeper, router, _, _ = parts
    folder_id = watch_drop(svc, keeper)
    sub = keeper.store.subscription("key-1", mail_resource(folder_id))
    router.handle({"value": [{"subscriptionId": sub["sub_id"], "clientState": sub["client_state"],
                              "changeType": "created", "resourceData": {"id": "never-seen"}}]})
    assert rows(svc, "never-seen") == [] and router.counts["woke"] == 1
    [line] = [r.getMessage() for r in caplog.records if r.name == "outlook_mcp.events"]
    assert f"resource={sub['resource']}" in line and "message=never-seen" in line and "reset=0" in line


def test_drop_created_notification_leaves_new_mode_rows_alone(fake, svc, parts):
    keeper, _, _, _ = parts
    new_args = {"folder": "Inbox"}
    svc.poll_new_mail("key-1", new_args)
    watch_drop(svc, keeper, folder="Inbox")
    mid = fake.add_message(subject="both", received="2026-10-07T10:01:00Z")
    assert svc.poll_new_mail("key-1", new_args)["mail"]["id"] == mid
    assert drop_poll(svc, "Inbox")["mail"]["id"] == mid
    new_scope = svc.watch_scope("key-1", new_args)[0]
    before = [r for r in rows(svc, mid) if r[0] == new_scope]
    fake.notify_created(mid)
    assert [r for r in rows(svc, mid) if r[0] == new_scope] == before == [(new_scope, 1, 1, 0, 0)]
    assert svc.poll_new_mail("key-1", new_args) == {"mail": None}      # "new" stays at-most-once
    assert drop_poll(svc, "Inbox")["mail"]["attempt"] == 1


def test_drop_created_notification_only_resets_its_own_folder(fake, svc, parts):
    keeper, _, _, _ = parts
    watch_drop(svc, keeper)
    other = "Other drop"
    drop_poll(svc, other)                                    # a drop watch of another folder, no subscription
    mid = fake.add_message(folder=DROP, subject="x", received="2026-10-01T08:00:00Z")
    assert drop_poll(svc)["mail"]["attempt"] == 1
    other_id = svc.watch_scope("key-1", {"folder": other, "mode": "drop"})[1]
    other_scope = svc.watch_scope("key-1", {"folder": other, "mode": "drop"})[0]
    svc.store.enqueue_drop("key-1", other_scope, [(mid, "2026-10-01T08:00:00Z")], baseline=True)
    fake.notify_created(mid)
    assert (other_scope, 1, 0, 1, 1) in rows(svc, mid)        # the other folder's row is untouched
    assert other_id != svc.watch_scope("key-1", {"folder": DROP, "mode": "drop"})[1]


def test_drop_created_notification_with_a_forged_client_state_resets_nothing(fake, svc, parts):
    keeper, router, _, clock = parts
    folder_id = watch_drop(svc, keeper)
    mid = fake.add_message(folder=DROP, subject="x", received="2026-10-01T08:00:00Z")
    assert drop_poll(svc)["mail"]["attempt"] == 1
    sub = keeper.store.subscription("key-1", mail_resource(folder_id))
    router.handle({"value": [{"subscriptionId": sub["sub_id"], "clientState": "forged",
                              "changeType": "created", "resourceData": {"id": mid}}]})
    assert router.counts["rejected"] == 1
    assert drop_poll(svc) == {"mail": None}                  # still delivered, within the lease
