"""MCP tool results: a success is structuredContent matching the tool's outputSchema; a failure is isError."""
import json

from server import TOOLS
from tests.test_server import app

SCHEMAS = {t["name"]: t["outputSchema"] for t in TOOLS}


def conforms(value, schema, path="$"):
    """The JSON Schema subset TOOLS uses: type (or a list of types), properties, required, items, enum."""
    types = schema.get("type")
    types = [types] if isinstance(types, str) else (types or [])
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    ok = not types or any(
        (isinstance(value, (int, float)) and not isinstance(value, bool)) if t == "number"
        else (isinstance(value, int) and not isinstance(value, bool)) if t == "integer"
        else isinstance(value, kinds[t]) for t in types)
    assert ok, f"{path}: {value!r} is not {types}"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} not in {schema['enum']}"
    if isinstance(value, dict):
        for name in schema.get("required", []):
            assert name in value, f"{path}: missing {name}"
        for name, sub in schema.get("properties", {}).items():
            if name in value:
                conforms(value[name], sub, f"{path}.{name}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            conforms(item, schema["items"], f"{path}[{i}]")


def raw(c, tool, args=None, key=None):
    headers = {"X-Outlook-Account": key} if key else {}
    return c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": tool, "arguments": args or {}}}).json()["result"]


def success(c, tool, args=None, key=None):
    """A successful call: structuredContent, the same JSON as text, matching the tool's outputSchema."""
    r = raw(c, tool, args, key)
    assert "isError" not in r, r
    assert r["structuredContent"] == json.loads(r["content"][0]["text"])
    conforms(r["structuredContent"], SCHEMAS[tool])
    return r["structuredContent"]


def test_every_tool_declares_an_object_output_schema():
    for t in TOOLS:
        assert t["outputSchema"]["type"] == "object", t["name"]
        assert t["description"].strip(), t["name"]


def test_every_tool_result_matches_its_output_schema(fake, http, cfg):
    c = app(fake, http, cfg)
    assert success(c, "connect_status", {"connect_id": "nope"}) == {"status": "unknown"}
    start = success(c, "connect_start", {"method": "device"})
    assert success(c, "connect_status", {"connect_id": start["connect_id"]})["status"] == "pending"
    fake.device_state = "connected"
    c.app.state.clock_offset[0] += 10
    key = success(c, "connect_status", {"connect_id": start["connect_id"]})["connection_key"]
    assert success(c, "whoami", key=key)["account"] == "me@contoso.com"
    assert {"Inbox"} <= {f["path"] for f in success(c, "list_folders", key=key)["folders"]}
    assert success(c, "poll_new_mail", {"folder": "Inbox"}, key=key) == {"mail": None}
    fake.add_message(subject="Invoice", received="2099-01-01T00:00:00Z",
                     attachments=[("a.txt", "text/plain", b"hello")])
    mail = success(c, "poll_new_mail", {"folder": "Inbox"}, key=key)["mail"]
    assert mail["subject"] == "Invoice" and mail["attachments"][0]["name"] == "a.txt"
    att = success(c, "get_attachment", {"mail_id": mail["id"], "attachment_id": mail["attachments"][0]["id"]}, key=key)
    assert att["text"].strip() == "hello"


def test_failures_are_tool_errors_carrying_code_and_message(fake, http, cfg):
    c = app(fake, http, cfg)
    for args, key, code in (({}, None, "not_connected"), ({}, "bogus", "reconnect_needed")):
        r = raw(c, "poll_new_mail", args, key)
        assert r["isError"] is True and "structuredContent" not in r
        err = json.loads(r["content"][0]["text"])
        assert err["error"] == code and err["message"]
