import dataclasses
import json
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from server import create_app


def app(fake, http, cfg):
    return TestClient(create_app(cfg, http=http, fake=fake))


def call(c, tool, args=None, key=None):
    headers = {"X-Outlook-Account": key} if key else {}
    r = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": tool, "arguments": args or {}}}).json()
    return json.loads(r["result"]["content"][0]["text"])


def connect(c, fake):
    s = call(c, "connect_start", {"method": "device"})
    fake.device_state = "connected"
    c.app.state.clock_offset[0] += 10
    return call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]


def test_full_flow_over_mcp(fake, http, cfg):
    c = app(fake, http, cfg)
    key = connect(c, fake)
    assert call(c, "whoami", key=key)["account"] == "me@contoso.com"
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key) == {"mail": None}
    fake.add_message(subject="Hello", received="2099-01-01T00:00:00Z")
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]["subject"] == "Hello"


def test_tools_without_or_with_unknown_key(fake, http, cfg):
    c = app(fake, http, cfg)
    assert call(c, "poll_new_mail", {})["error"] == "not_connected"
    assert call(c, "poll_new_mail", {}, key="bogus")["error"] == "reconnect_needed"
    assert call(c, "whoami", key="bogus")["error"] == "reconnect_needed"


def test_redirect_endpoints(fake, http, cfg):
    c = app(fake, http, cfg)
    s = call(c, "connect_start")
    r = c.get(f"/auth/start?connect_id={s['connect_id']}", follow_redirects=False)
    assert r.status_code == 307 and "/oauth2/v2.0/authorize" in r.headers["location"]
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert "Connected" in c.get(f"/auth/callback?code=c1&state={state}").text
    assert call(c, "connect_status", {"connect_id": s["connect_id"]})["status"] == "connected"


# JSON-RPC transport ----------------------------------------------------------------------

def test_notification_gets_202_and_empty_body(fake, http, cfg):
    r = app(fake, http, cfg).post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""


def test_invalid_json_is_parse_error(fake, http, cfg):
    r = app(fake, http, cfg).post("/mcp", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["error"]["code"] == -32700 and r.json()["id"] is None


def test_batch_or_non_object_is_invalid_request(fake, http, cfg):
    c = app(fake, http, cfg)
    for body in ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], "ping", 3):
        r = c.post("/mcp", json=body)
        assert r.status_code == 200 and r.json()["error"]["code"] == -32600


def test_tools_list_echoes_id_on_both_routes(fake, http, cfg):
    c = app(fake, http, cfg)
    for path, req_id in (("/", 7), ("/mcp", "abc")):
        r = c.post(path, json={"jsonrpc": "2.0", "id": req_id, "method": "tools/list"}).json()
        assert r["id"] == req_id and len(r["result"]["tools"]) == 6


# errors at the tool boundary -------------------------------------------------------------

def test_connect_start_auth_failure_is_a_tool_result(fake, cfg):
    def handler(request):
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(400, json={"error": "invalid_client", "error_description": "AADSTS7000215"})
        return fake.handler(request)
    c = app(fake, httpx.Client(transport=httpx.MockTransport(handler)), cfg)
    assert call(c, "connect_start") == {"error": "invalid_client", "message": "AADSTS7000215"}


def test_connect_start_network_failure_is_a_tool_result(fake, cfg):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    c = app(fake, httpx.Client(transport=httpx.MockTransport(handler)), cfg)
    assert call(c, "connect_start")["error"] == "network_error"


def _me_fails(fake, monkeypatch, status=500):
    real = fake._graph
    monkeypatch.setattr(fake, "_graph", lambda path, q: httpx.Response(status, json={"error": {}})
                        if path == "/me" else real(path, q))


def test_identity_lookup_failure_after_device_sign_in_is_a_tool_result(fake, http, cfg, monkeypatch):
    c = app(fake, http, cfg)
    s = call(c, "connect_start")
    fake.device_state = "connected"
    _me_fails(fake, monkeypatch)
    result = call(c, "connect_status", {"connect_id": s["connect_id"]})
    assert result["error"] == "graph_error" and set(result) == {"error", "message"}


def test_callback_failure_is_a_short_page_without_tokens(fake, http, cfg, monkeypatch):
    c = app(fake, http, cfg)
    s = call(c, "connect_start")
    loc = c.get(f"/auth/start?connect_id={s['connect_id']}", follow_redirects=False).headers["location"]
    state = parse_qs(urlparse(loc).query)["state"][0]
    _me_fails(fake, monkeypatch)
    r = c.get(f"/auth/callback?code=c1&state={state}")
    assert "Sign-in failed" in r.text and "graph_error" in r.text
    issued = fake.access_tokens | fake.refresh_tokens
    assert issued and not [t for t in issued if t in r.text]


def test_callback_escapes_the_error_description(fake, http, cfg):
    r = app(fake, http, cfg).get("/auth/callback?error_description=<script>x()</script>")
    assert "Sign-in failed" in r.text and "<script>" not in r.text


def test_auth_start_without_public_url_is_a_page_not_a_500(fake, http, cfg):
    c = app(fake, http, dataclasses.replace(cfg, public_url=""))
    s = call(c, "connect_start")
    r = c.get(f"/auth/start?connect_id={s['connect_id']}", follow_redirects=False)
    assert r.status_code == 400 and "OUTLOOK_PUBLIC_URL" in r.text


def test_create_app_does_not_prune(fake, http, cfg, monkeypatch):
    def no_prune(self, days=30):
        raise AssertionError("prune could re-deliver mail that delta re-sends")
    monkeypatch.setattr("store.Store.prune", no_prune)
    assert call(app(fake, http, cfg), "connect_start")["user_code"]


# fake mode -------------------------------------------------------------------------------

def test_fake_mode_endpoints(cfg):
    c = TestClient(create_app(dataclasses.replace(cfg, fake_graph=True)))
    s = call(c, "connect_start")
    key = call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key) == {"mail": None}
    mid = c.post("/fake/messages", json={"subject": "Smoke", "sender": "billing@acme.com", "received": "2099-01-01T00:00:00Z",
                                         "attachments": [{"name": "a.txt", "text": "x"}]}).json()["id"]
    mail = call(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]
    assert mail["id"] == mid and mail["attachments"][0]["name"] == "a.txt"
    assert not [p for _, p in c.get("/fake/requests").json()["requests"] if "$value" in p]
    assert c.post("/fake/device", json={"state": "pending"}).json() == {"state": "pending"}


def test_fake_seed(cfg, tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"subject": "Seeded", "received": "2099-01-01T00:00:00Z"}]))
    c = TestClient(create_app(dataclasses.replace(cfg, fake_graph=True, fake_seed=str(seed))))
    s = call(c, "connect_start")
    key = call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]["subject"] == "Seeded"


def test_fake_endpoints_absent_outside_fake_mode(fake, http, cfg):
    assert app(fake, http, cfg).get("/fake/requests").status_code == 404
