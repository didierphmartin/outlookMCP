from fastapi.testclient import TestClient

from config import AppConfig
from server import TOOLS, create_app


def client(tmp_path):
    cfg = AppConfig(client_id="cid", secret_key=AppConfig.new_secret_key(), db_path=str(tmp_path / "o.sqlite"))
    return TestClient(create_app(cfg))


def rpc(c, method, params=None, req_id=1):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}).json()


def test_initialize_and_tools_list(tmp_path):
    c = client(tmp_path)
    init = rpc(c, "initialize")
    assert init["result"]["serverInfo"]["name"] == "outlook"
    names = [t["name"] for t in rpc(c, "tools/list")["result"]["tools"]]
    assert names == ["connect_start", "connect_status", "whoami", "list_folders", "poll_new_mail", "get_attachment",
                     "move_mail", "forward_mail", "wait_for_new"]


def test_get_attachment_description_carries_the_triage_rule():
    desc = next(t for t in TOOLS if t["name"] == "get_attachment")["description"]
    assert "after" in desc and "triage" in desc


def test_unknown_method_and_tool(tmp_path):
    c = client(tmp_path)
    assert rpc(c, "nope")["error"]["code"] == -32601
    assert rpc(c, "tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32601


def test_notifications_are_acknowledged(tmp_path):
    r = client(tmp_path).post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""


def test_from_env_requires_ids(monkeypatch):
    monkeypatch.delenv("OUTLOOK_CLIENT_ID", raising=False)
    monkeypatch.delenv("OUTLOOK_FAKE_GRAPH", raising=False)
    import pytest
    with pytest.raises(SystemExit, match="OUTLOOK_CLIENT_ID"):
        AppConfig.from_env()
