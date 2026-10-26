import dataclasses
import sqlite3

import httpx
import pytest

from graph import AppTokenSource, GraphError
from tests.test_server import app, call


@pytest.fixture
def org_cfg(cfg):
    return dataclasses.replace(cfg, org_tenant="tid-1", org_client_id="org-cid", org_client_secret="org-secret",
                               org_group="vip-group")


def rows(cfg):
    return sqlite3.connect(cfg.db_path).execute("SELECT account, kind FROM connections").fetchall()


def test_org_method_offered_only_when_configured(fake, http, cfg, org_cfg):
    def enum(c):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        tool = next(t for t in r["result"]["tools"] if t["name"] == "connect_start")
        assert "verification_uri" not in tool["outputSchema"]["required"]
        return tool["inputSchema"]["properties"]["method"]["enum"]
    assert enum(app(fake, http, cfg)) == ["device"]
    assert enum(app(fake, http, org_cfg)) == ["device", "organization"]


def test_org_connect_hands_out_a_key_once(fake, http, org_cfg):
    fake.add_mailbox("u-alice", "alice@contoso.com", "Alice")
    c = app(fake, http, org_cfg)
    s = call(c, "connect_start", {"method": "organization"})
    assert s["status"] == "connected"
    done = call(c, "connect_status", {"connect_id": s["connect_id"]})
    assert done["status"] == "connected" and done["account"] == "VIP Mailboxes"
    assert done["credential_header"] == "X-Outlook-Account" and len(done["connection_key"]) >= 32
    assert call(c, "connect_status", {"connect_id": s["connect_id"]}) == {"status": "connected"}
    assert rows(org_cfg) == [("VIP Mailboxes", "organization")]


def test_org_connect_fails_when_not_configured(fake, http, cfg):
    r = call(app(fake, http, cfg), "connect_start", {"method": "organization"})
    assert r["error"] == "not_configured"


def test_org_connect_fails_cleanly_on_bad_secret(fake, http, org_cfg):
    fake.app_secret = "something-else"
    r = call(app(fake, http, org_cfg), "connect_start", {"method": "organization"})
    assert r["error"] == "reconnect_needed"
    assert rows(org_cfg) == []


def test_org_connect_fails_when_group_unreadable(fake, http, org_cfg):
    cfg = dataclasses.replace(org_cfg, org_group="no-such-group")
    r = call(app(fake, http, cfg), "connect_start", {"method": "organization"})
    assert "error" in r and r["error"] != "not_configured"
    assert rows(cfg) == []


def test_app_token_cached_and_refreshed_before_expiry(fake, http, org_cfg):
    t = [1000.0]
    src = AppTokenSource(http, org_cfg, clock=lambda: t[0])
    first = src.access_token()
    t[0] += 3000
    assert src.access_token() == first
    t[0] += 600                                  # inside the last 60 s of the hour
    second = src.access_token()
    assert second != first
    assert src.access_token(force=True) != second
    assert sum(1 for m, p in fake.requests if p.endswith("/token")) == 3


def test_app_token_failures_are_graph_errors(fake, http, org_cfg):
    fake.app_secret = "x"
    with pytest.raises(GraphError) as e:
        AppTokenSource(http, org_cfg).access_token()
    assert e.value.code == "reconnect_needed"
    down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    with pytest.raises(GraphError) as e:
        AppTokenSource(down, org_cfg).access_token()
    assert e.value.code == "network_error"


def test_user_device_connect_unchanged(fake, http, cfg):
    from tests.test_server import connect
    c = app(fake, http, cfg)
    key = connect(c, fake)
    assert rows(cfg) == [("me@contoso.com", "user")]
    assert call(c, "whoami", {}, key=key).get("error") is None
