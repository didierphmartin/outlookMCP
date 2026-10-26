"""wait_for_new on an organization connection: one subscription per member mailbox's folder, ready on any."""
import dataclasses
import threading
import time

import pytest

from subscriptions import folder_of, mail_resource
from tests.test_events import HUB
from tests.test_org_connect import org_cfg  # noqa: F401 -- the fixture
from tests.test_org_mail import DROP, drop_folder, later
from tests.test_server import app, call, connect


@pytest.fixture
def ocfg(org_cfg):  # noqa: F811
    return dataclasses.replace(org_cfg, notification_url=HUB)


def org_app(fake, http, cfg):
    fake.add_mailbox("u-alice", "alice@contoso.com", "Alice Martin")
    fake.add_mailbox("u-bob", "bob@contoso.com", "Bob Stone")
    c = app(fake, http, cfg)
    s = call(c, "connect_start", {"method": "organization"})
    return c, call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]


def wait(c, key, timeout=5):
    return call(c, "wait_for_new", {"args": DROP, "timeout_s": timeout}, key=key)


def subscribed(fake):
    return sorted(s["resource"] for s in fake.subscriptions.values())


def test_folder_of_parses_both_forms():
    assert mail_resource("f-1") == "me/mailFolders('f-1')/messages"
    assert mail_resource("f-1", "u-bob") == "users/u-bob/mailFolders('f-1')/messages"
    assert folder_of(mail_resource("f-1")) == "f-1"
    assert folder_of(mail_resource("f-1", "u-bob")) == "f-1"
    assert folder_of("users/u-bob/messages") is None and folder_of("") is None


def test_wait_ready_on_event_from_any_mailbox(fake, http, ocfg):
    c, key = org_app(fake, http, ocfg)
    assert wait(c, key) == {"status": "ready", "via": "fallback", "events": "on"}   # first wait after start
    fid = drop_folder(fake)
    assert subscribed(fake) == [mail_resource(fid, "u-alice"), mail_resource(fid, "u-bob")]
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}                 # baseline
    assert wait(c, key, timeout=0.3) == {"status": "timeout", "events": "on"}

    threading.Timer(0.3, lambda: fake.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob")).start()
    t0 = time.monotonic()
    assert wait(c, key, timeout=10) == {"status": "ready", "via": "event", "events": "on"}
    assert time.monotonic() - t0 < 5
    assert call(c, "poll_new_mail", DROP, key=key)["mail"]["mailbox"] == "bob@contoso.com"

    threading.Timer(0.3, lambda: fake.add_message(folder="To SharePoint", subject="alice", mailbox="u-alice")).start()
    assert wait(c, key, timeout=10)["via"] == "event"
    assert len(subscribed(fake)) == 2                                                # reused, not re-created


def test_wait_uses_org_fallback_interval(fake, http, ocfg):
    c, key = org_app(fake, http, dataclasses.replace(ocfg, org_fallback_s=1, watch_fallback_s=900))
    assert wait(c, key)["via"] == "fallback"
    t0 = time.monotonic()
    assert wait(c, key, timeout=10) == {"status": "ready", "via": "fallback", "events": "on"}
    assert 0.5 < time.monotonic() - t0 < 5
    user = connect(c, fake)                                  # a user connection keeps watch_fallback_s
    assert call(c, "wait_for_new", {"args": {"folder": "Inbox"}, "timeout_s": 5}, key=user)["via"] == "fallback"
    assert call(c, "wait_for_new", {"args": {"folder": "Inbox"}, "timeout_s": 1.5}, key=user)["status"] == "timeout"


def test_subscription_failure_on_one_mailbox_keeps_others(fake, http, ocfg):
    """Alice's folder reads fine but Graph refuses a subscription on her mailbox: her folder is polled on the
    timer, Bob's still wakes the wait, and the wait itself is no error."""
    c, key = org_app(fake, http, ocfg)
    fake.forbid_mailbox("u-alice")
    select = fake._select_mailbox

    def reads_allowed(path):
        saved = set(fake.forbidden_mailboxes)
        fake.forbidden_mailboxes.clear()
        try:
            return select(path)
        finally:
            fake.forbidden_mailboxes |= saved
    fake._select_mailbox = reads_allowed
    first = wait(c, key)
    assert first["status"] == "ready" and first["events"] == "off"
    assert "u-alice" in first["reason"] and "forbidden" in first["reason"]
    fid = drop_folder(fake)
    assert subscribed(fake) == [mail_resource(fid, "u-bob")]
    store = c.app.state.keeper.store
    assert store.subscription(key, mail_resource(fid, "u-alice"))["sub_id"] is None
    call(c, "poll_new_mail", DROP, key=key)
    threading.Timer(0.3, lambda: fake.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob")).start()
    assert wait(c, key, timeout=10)["via"] == "event"

    fake._select_mailbox = select                            # now out of reach entirely: skipped, still no error
    assert wait(c, key, timeout=0.2)["status"] == "timeout"


def test_left_mailbox_subscription_dropped(fake, http, ocfg):
    c, key = org_app(fake, http, ocfg)
    wait(c, key)
    fid = drop_folder(fake)
    alice = c.app.state.keeper.store.subscription(key, mail_resource(fid, "u-alice"))["sub_id"]
    fake.mailboxes["u-alice"]["in_group"] = False
    later(c)
    wait(c, key, timeout=0.2)
    assert subscribed(fake) == [mail_resource(fid, "u-bob")] and alice not in fake.subscriptions
    assert c.app.state.keeper.store.subscription(key, mail_resource(fid, "u-alice")) is None
