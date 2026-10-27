import dataclasses
import json
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from server import create_app


def app(mock, http, cfg):
    return TestClient(create_app(cfg, http=http, mock=mock))


def call(c, tool, args=None, key=None):
    headers = {"X-Outlook-Account": key} if key else {}
    r = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": tool, "arguments": args or {}}}).json()
    return json.loads(r["result"]["content"][0]["text"])


def connect(c, mock):
    s = call(c, "connect_start", {"method": "device"})
    mock.device_state = "connected"
    c.app.state.clock_offset[0] += 10
    return call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]


def test_full_flow_over_mcp(mock, http, cfg):
    c = app(mock, http, cfg)
    key = connect(c, mock)
    assert call(c, "whoami", key=key)["account"] == "me@contoso.com"
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key) == {"mail": None}
    mock.add_message(subject="Hello", received="2099-01-01T00:00:00Z")
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]["subject"] == "Hello"


def test_tools_without_or_with_unknown_key(mock, http, cfg):
    c = app(mock, http, cfg)
    assert call(c, "poll_new_mail", {})["error"] == "not_connected"
    assert call(c, "poll_new_mail", {}, key="bogus")["error"] == "reconnect_needed"
    assert call(c, "whoami", key="bogus")["error"] == "reconnect_needed"


def test_redirect_endpoints(mock, http, cfg):
    c = app(mock, http, cfg)
    s = call(c, "connect_start")
    r = c.get(f"/auth/start?connect_id={s['connect_id']}", follow_redirects=False)
    assert r.status_code == 307 and "/oauth2/v2.0/authorize" in r.headers["location"]
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert "Connected" in c.get(f"/auth/callback?code=c1&state={state}").text
    assert call(c, "connect_status", {"connect_id": s["connect_id"]})["status"] == "connected"


# JSON-RPC transport ----------------------------------------------------------------------

def test_notification_gets_202_and_empty_body(mock, http, cfg):
    r = app(mock, http, cfg).post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""


def test_invalid_json_is_parse_error(mock, http, cfg):
    r = app(mock, http, cfg).post("/mcp", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["error"]["code"] == -32700 and r.json()["id"] is None


def test_batch_or_non_object_is_invalid_request(mock, http, cfg):
    c = app(mock, http, cfg)
    for body in ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], "ping", 3):
        r = c.post("/mcp", json=body)
        assert r.status_code == 200 and r.json()["error"]["code"] == -32600


def test_tools_list_echoes_id_on_both_routes(mock, http, cfg):
    c = app(mock, http, cfg)
    for path, req_id in (("/", 7), ("/mcp", "abc")):
        r = c.post(path, json={"jsonrpc": "2.0", "id": req_id, "method": "tools/list"}).json()
        assert r["id"] == req_id and len(r["result"]["tools"]) == 10


# errors at the tool boundary -------------------------------------------------------------

def test_connect_start_auth_failure_is_a_tool_result(mock, cfg):
    def handler(request):
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(400, json={"error": "invalid_client", "error_description": "AADSTS7000215"})
        return mock.handler(request)
    c = app(mock, httpx.Client(transport=httpx.MockTransport(handler)), cfg)
    assert call(c, "connect_start") == {"error": "invalid_client", "message": "AADSTS7000215"}


def test_connect_start_network_failure_is_a_tool_result(mock, cfg):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    c = app(mock, httpx.Client(transport=httpx.MockTransport(handler)), cfg)
    assert call(c, "connect_start")["error"] == "network_error"


def _me_fails(mock, monkeypatch, status=500):
    real = mock._graph
    monkeypatch.setattr(mock, "_graph", lambda path, q: httpx.Response(status, json={"error": {}})
                        if path == "/me" else real(path, q))


def test_identity_lookup_failure_after_device_sign_in_is_a_tool_result(mock, http, cfg, monkeypatch):
    c = app(mock, http, cfg)
    s = call(c, "connect_start")
    mock.device_state = "connected"
    _me_fails(mock, monkeypatch)
    result = call(c, "connect_status", {"connect_id": s["connect_id"]})
    assert result["status"] == "failed" and "graph_error" in result["message"]


def test_callback_failure_is_a_short_page_without_tokens(mock, http, cfg, monkeypatch):
    c = app(mock, http, cfg)
    s = call(c, "connect_start")
    loc = c.get(f"/auth/start?connect_id={s['connect_id']}", follow_redirects=False).headers["location"]
    state = parse_qs(urlparse(loc).query)["state"][0]
    _me_fails(mock, monkeypatch)
    r = c.get(f"/auth/callback?code=c1&state={state}")
    assert "Sign-in failed" in r.text and "graph_error" in r.text
    issued = mock.access_tokens | mock.refresh_tokens
    assert issued and not [t for t in issued if t in r.text]


def test_callback_escapes_the_error_description(mock, http, cfg):
    r = app(mock, http, cfg).get("/auth/callback?error_description=<script>x()</script>")
    assert "Sign-in failed" in r.text and "<script>" not in r.text


def test_auth_start_without_public_url_is_a_page_not_a_500(mock, http, cfg):
    c = app(mock, http, dataclasses.replace(cfg, public_url=""))
    s = call(c, "connect_start")
    r = c.get(f"/auth/start?connect_id={s['connect_id']}", follow_redirects=False)
    assert r.status_code == 400 and "OUTLOOK_PUBLIC_URL" in r.text


def test_create_app_does_not_prune(mock, http, cfg, monkeypatch):
    def no_prune(self, days=30):
        raise AssertionError("prune could re-deliver mail that delta re-sends")
    monkeypatch.setattr("store.Store.prune", no_prune)
    assert call(app(mock, http, cfg), "connect_start")["user_code"]


# mock mode -------------------------------------------------------------------------------

def test_mock_mode_endpoints(cfg):
    c = TestClient(create_app(dataclasses.replace(cfg, mock_graph=True)))
    s = call(c, "connect_start")
    key = call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key) == {"mail": None}
    mid = c.post("/mock/messages", json={"subject": "Smoke", "sender": "billing@acme.com", "received": "2099-01-01T00:00:00Z",
                                         "attachments": [{"name": "a.txt", "text": "x"}]}).json()["id"]
    mail = call(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]
    assert mail["id"] == mid and mail["attachments"][0]["name"] == "a.txt"
    assert not [p for _, p in c.get("/mock/requests").json()["requests"] if "$value" in p]
    assert c.post("/mock/device", json={"state": "pending"}).json() == {"state": "pending"}


def test_mock_seed(cfg, tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"subject": "Seeded", "received": "2099-01-01T00:00:00Z"}]))
    c = TestClient(create_app(dataclasses.replace(cfg, mock_graph=True, mock_seed=str(seed))))
    s = call(c, "connect_start")
    key = call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]
    assert call(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]["subject"] == "Seeded"


def test_non_object_params_is_invalid_params(mock, http, cfg):
    c = app(mock, http, cfg)
    for params in ([1, 2], "x"):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": params})
        assert r.status_code == 200 and r.json()["error"]["code"] == -32602 and r.json()["id"] == 3
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "whoami", "arguments": [1]}})
    assert r.status_code == 200 and r.json()["error"]["code"] == -32602


def test_mock_endpoints_absent_outside_mock_mode(mock, http, cfg):
    assert app(mock, http, cfg).get("/mock/requests").status_code == 404


# final review fixes ------------------------------------------------------------------------

def test_tool_calls_run_concurrently(mock, http, cfg, monkeypatch):
    """A slow tool call (a blocking Graph request) must not stall the others."""
    import threading

    import anyio
    released = threading.Event()

    def status(self, connect_id):
        if connect_id == "slow":
            return {"released": released.wait(3)}
        released.set()
        return {"status": "unknown"}
    monkeypatch.setattr("auth.Connector.status", status)
    application = create_app(cfg, http=http, mock=mock)

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://t") as client:
            async def one(cid):
                r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": cid, "method": "tools/call",
                                                    "params": {"name": "connect_status", "arguments": {"connect_id": cid}}})
                return json.loads(r.json()["result"]["content"][0]["text"])
            results = {}

            async def run(cid):
                results[cid] = await one(cid)
            async with anyio.create_task_group() as tg:
                tg.start_soon(run, "slow")
                await anyio.sleep(0.05)
                tg.start_soon(run, "fast")
            return results
    assert anyio.run(main)["slow"] == {"released": True}


def test_changed_secret_key_is_reconnect_needed_over_mcp(mock, http, cfg):
    from config import AppConfig
    c = app(mock, http, cfg)
    key = connect(c, mock)
    c2 = app(mock, http, dataclasses.replace(cfg, secret_key=AppConfig.new_secret_key()))
    for tool in ("whoami", "poll_new_mail"):
        assert call(c2, tool, {"folder": "Inbox"}, key=key)["error"] == "reconnect_needed"
