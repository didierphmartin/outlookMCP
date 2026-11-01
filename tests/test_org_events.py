"""wait_for_new on an organization connection: one subscription per member mailbox's folder, ready on any."""
import dataclasses
import threading
import time

import httpx
import pytest

from subscriptions import folder_of, mail_resource, mailbox_folder_of
from tests.test_events import HUB
from tests.test_org_connect import org_cfg  # noqa: F401 -- the fixture
from tests.test_org_mail import DROP, drop_folder, later
from tests.test_server import app, call, connect


@pytest.fixture
def ocfg(org_cfg):  # noqa: F811
    return dataclasses.replace(org_cfg, notification_url=HUB)


def org_app(mock, http, cfg):
    mock.add_mailbox("u-alice", "alice@contoso.com", "Alice Martin")
    mock.add_mailbox("u-bob", "bob@contoso.com", "Bob Stone")
    c = app(mock, http, cfg)
    s = call(c, "connect_start", {"method": "organization"})
    return c, call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]


def wait(c, key, timeout=5):
    return call(c, "wait_for_new", {"args": DROP, "timeout_s": timeout}, key=key)


def subscribed(mock):
    return sorted(s["resource"] for s in mock.subscriptions.values())


def test_folder_of_parses_both_forms():
    assert mail_resource("f-1") == "me/mailFolders('f-1')/messages"
    assert mail_resource("f-1", "u-bob") == "users/u-bob/mailFolders('f-1')/messages"
    assert folder_of(mail_resource("f-1")) == "f-1"
    assert folder_of(mail_resource("f-1", "u-bob")) == "f-1"
    assert folder_of("users/u-bob/messages") is None and folder_of("") is None
    assert mailbox_folder_of(mail_resource("f-1")) == (None, "f-1")
    assert mailbox_folder_of(mail_resource("f-1", "u-bob")) == ("u-bob", "f-1")
    assert mailbox_folder_of("subscriptions") is None


def test_wait_ready_on_event_from_any_mailbox(mock, http, ocfg):
    c, key = org_app(mock, http, ocfg)
    assert wait(c, key) == {"status": "ready", "via": "fallback", "events": "on"}   # first wait after start
    fid = drop_folder(mock)
    assert subscribed(mock) == [mail_resource(fid, "u-alice"), mail_resource(fid, "u-bob")]
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}                 # baseline
    assert wait(c, key, timeout=0.3) == {"status": "timeout", "events": "on"}

    threading.Timer(0.3, lambda: mock.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob")).start()
    t0 = time.monotonic()
    assert wait(c, key, timeout=10) == {"status": "ready", "via": "event", "events": "on"}
    assert time.monotonic() - t0 < 5
    assert call(c, "poll_new_mail", DROP, key=key)["mail"]["mailbox"] == "bob@contoso.com"

    threading.Timer(0.3, lambda: mock.add_message(folder="To SharePoint", subject="alice", mailbox="u-alice")).start()
    assert wait(c, key, timeout=10)["via"] == "event"
    assert len(subscribed(mock)) == 2                                                # reused, not re-created


def test_wait_uses_org_fallback_interval(mock, http, ocfg):
    c, key = org_app(mock, http, dataclasses.replace(ocfg, org_fallback_s=1, watch_fallback_s=900))
    assert wait(c, key)["via"] == "fallback"
    t0 = time.monotonic()
    assert wait(c, key, timeout=10) == {"status": "ready", "via": "fallback", "events": "on"}
    assert 0.5 < time.monotonic() - t0 < 5
    user = connect(c, mock)                                  # a user connection keeps watch_fallback_s
    assert call(c, "wait_for_new", {"args": {"folder": "Inbox"}, "timeout_s": 5}, key=user)["via"] == "fallback"
    assert call(c, "wait_for_new", {"args": {"folder": "Inbox"}, "timeout_s": 1.5}, key=user)["status"] == "timeout"


def test_subscription_failure_on_one_mailbox_keeps_others(mock, http, ocfg):
    """Alice's folder reads fine but Graph refuses a subscription on her mailbox: her folder is polled on the
    timer, Bob's still wakes the wait, and the wait itself is no error."""
    c, key = org_app(mock, http, ocfg)
    mock.forbid_mailbox("u-alice")
    select = mock._select_mailbox

    def reads_allowed(path):
        saved = set(mock.forbidden_mailboxes)
        mock.forbidden_mailboxes.clear()
        try:
            return select(path)
        finally:
            mock.forbidden_mailboxes |= saved
    mock._select_mailbox = reads_allowed
    first = wait(c, key)
    assert first["status"] == "ready" and first["events"] == "off"
    assert "u-alice" in first["reason"] and "forbidden" in first["reason"]
    fid = drop_folder(mock)
    assert subscribed(mock) == [mail_resource(fid, "u-bob")]
    store = c.app.state.keeper.store
    assert store.subscription(key, mail_resource(fid, "u-alice"))["sub_id"] is None
    call(c, "poll_new_mail", DROP, key=key)
    threading.Timer(0.3, lambda: mock.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob")).start()
    assert wait(c, key, timeout=10)["via"] == "event"

    mock._select_mailbox = select                            # now out of reach entirely: skipped, still no error
    assert wait(c, key, timeout=0.2)["status"] == "timeout"


def test_left_mailbox_subscription_dropped(mock, http, ocfg):
    c, key = org_app(mock, http, ocfg)
    wait(c, key)
    fid = drop_folder(mock)
    alice = c.app.state.keeper.store.subscription(key, mail_resource(fid, "u-alice"))["sub_id"]
    mock.mailboxes["u-alice"]["in_group"] = False
    later(c)
    wait(c, key, timeout=0.2)
    assert subscribed(mock) == [mail_resource(fid, "u-bob")] and alice not in mock.subscriptions
    assert c.app.state.keeper.store.subscription(key, mail_resource(fid, "u-alice")) is None


def test_left_mailbox_drop_failure_never_fails_the_wait(mock, http, ocfg, caplog):
    c, key = org_app(mock, http, ocfg)
    wait(c, key)
    keeper = c.app.state.keeper

    def unreachable(_key):
        raise httpx.ConnectError("graph unreachable")
    keeper.graph_for = unreachable                           # the DELETE of Alice's subscription cannot go out
    mock.mailboxes["u-alice"]["in_group"] = False
    later(c)
    with caplog.at_level("WARNING", logger="outlook_mcp"):
        assert wait(c, key, timeout=0.2)["status"] in ("ready", "timeout")
    assert "u-alice" in caplog.text and "graph unreachable" in caplog.text


# "created" notifications in drop mode --------------------------------------------------------
def exhaust(c, key):
    """Claims the one drop email until its attempts run out (3, each after the lease); returns its id."""
    ids = set()
    for attempt in (1, 2, 3):
        mail = call(c, "poll_new_mail", DROP, key=key)["mail"]
        assert mail["attempt"] == attempt
        ids.add(mail["id"])
        later(c, 600)
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}
    [mail_id] = ids
    return mail_id


def created(c, key, resource, mid):
    """Graph's "created" notification on this connection's subscription of resource, naming message mid."""
    sub = c.app.state.keeper.store.subscription(key, resource)
    c.app.state.router.handle({"value": [{"subscriptionId": sub["sub_id"], "clientState": sub["client_state"],
                                          "changeType": "created", "resourceData": {"id": mid}}]})


def test_org_drop_created_notification_retries_an_exhausted_email(mock, http, ocfg):
    c, key = org_app(mock, http, ocfg)
    wait(c, key)
    call(c, "poll_new_mail", DROP, key=key)                                          # baseline
    mid = mock.add_message(folder="To SharePoint", subject="vip", mailbox="u-bob")
    assert exhaust(c, key) == f"u-bob~{mid}"
    mock.notify_created(mid)                                 # dragged out and back into Bob's drop folder
    mail = call(c, "poll_new_mail", DROP, key=key)["mail"]
    assert mail["id"] == f"u-bob~{mid}" and mail["attempt"] == 1


def test_created_notification_never_crosses_connections(mock, http, ocfg):
    """Same folder id, same message id on both sides: a user's me/... notification never resets or wakes the
    organization's scope, and an organization mailbox's never touches the user's."""
    c, org = org_app(mock, http, ocfg)
    user = connect(c, mock)
    for key in (org, user):
        wait(c, key)
        call(c, "poll_new_mail", DROP, key=key)                                      # baselines
    mb = mock.add_message(folder="To SharePoint", subject="vip", mailbox="u-bob")
    mu = mock.add_message(folder="To SharePoint", subject="mine")
    for attempt in (1, 2, 3):
        assert call(c, "poll_new_mail", DROP, key=org)["mail"]["attempt"] == attempt
        assert call(c, "poll_new_mail", DROP, key=user)["mail"]["attempt"] == attempt
        later(c, 600)
    fid, store, wakes = drop_folder(mock), c.app.state.keeper.store, c.app.state.wakes
    org_keys = [(store.key_id(org), mail_resource(fid, b)) for b in ("u-alice", "u-bob")]
    user_key = (store.key_id(user), mail_resource(fid))
    wakes.take_any([*org_keys, user_key])

    created(c, user, mail_resource(fid), mb)                 # the org's message id, on the user's subscription
    assert call(c, "poll_new_mail", DROP, key=org) == {"mail": None}
    assert not wakes.take_any(org_keys) and wakes.take_any([user_key])               # only its own folder woke
    created(c, org, mail_resource(fid, "u-bob"), mu)         # the user's message id, on Bob's subscription
    assert call(c, "poll_new_mail", DROP, key=user) == {"mail": None}
    assert not wakes.take_any([user_key]) and wakes.take_any(org_keys)

    created(c, org, mail_resource(fid, "u-bob"), mb)         # its own subscription: reset and woken
    assert wakes.take_any(org_keys)
    assert call(c, "poll_new_mail", DROP, key=org)["mail"]["attempt"] == 1


def test_empty_group_is_an_error_naming_the_group(mock, http, ocfg):
    """A group with no member mailbox watches nothing: the wait says so instead of looking like a quiet mailbox."""
    mock.add_mailbox("u-carol", "carol@contoso.com", "Carol Diaz", in_group=False)
    c = app(mock, http, ocfg)
    s = call(c, "connect_start", {"method": "organization"})
    key = call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]
    r = wait(c, key)
    assert r["error"] == "no_mailboxes"
    assert "vip-group" in r["message"]
    assert subscribed(mock) == []
