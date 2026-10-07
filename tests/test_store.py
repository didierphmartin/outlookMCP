import sqlite3

from config import AppConfig
from store import Store


def make(tmp_path, clock=lambda: 1000.0):
    return Store(str(tmp_path / "s.sqlite"), AppConfig.new_secret_key(), clock=clock)


def test_refresh_token_is_encrypted_at_rest(tmp_path):
    s = make(tmp_path)
    s.save_connection("key-1", "me@contoso.com", "tid", "RT-SECRET")
    assert s.connection("key-1")["refresh_token"] == "RT-SECRET"
    raw = sqlite3.connect(tmp_path / "s.sqlite").execute("SELECT * FROM connections").fetchall()
    assert "RT-SECRET" not in repr(raw) and "key-1" not in repr(raw)


def test_unknown_connection(tmp_path):
    assert make(tmp_path).connection("nope") is None


def test_connection_key_is_handed_out_once(tmp_path):
    s = make(tmp_path)
    s.create_connect("c1", {"device_code": "dc"}, expires_at=2000)
    s.finish_connect("c1", "connected", "key-1")
    assert s.take_connection_key("c1") == "key-1"
    assert s.take_connection_key("c1") is None
    assert s.connect("c1")["status"] == "connected"


def test_queue_is_oldest_first_and_idempotent(tmp_path):
    s = make(tmp_path)
    s.enqueue("k", "inbox|f", [("m2", "2026-10-07T10:02:00Z"), ("m1", "2026-10-07T10:01:00Z")])
    s.enqueue("k", "inbox|f", [("m1", "2026-10-07T10:01:00Z")])   # duplicate ignored
    assert s.next_queued("k", "inbox|f") == "m1"
    s.mark_delivered("k", "inbox|f", "m1")
    s.enqueue("k", "inbox|f", [("m1", "2026-10-07T10:01:00Z")])   # delivered stays delivered
    assert s.next_queued("k", "inbox|f") == "m2"
    assert s.next_queued("k", "other") is None


def test_cursor_and_restart(tmp_path):
    key = AppConfig.new_secret_key()
    s = Store(str(tmp_path / "s.sqlite"), key)
    s.save_cursor("k", "inbox|f", "https://delta/1", "2026-10-07T10:00:00Z")
    s.enqueue("k", "inbox|f", [("m1", "2026-10-07T10:01:00Z")])
    s2 = Store(str(tmp_path / "s.sqlite"), key)
    assert s2.cursor("k", "inbox|f") == {"delta_link": "https://delta/1", "since": "2026-10-07T10:00:00Z"}
    assert s2.next_queued("k", "inbox|f") == "m1"


def test_prune_drops_old_delivered(tmp_path):
    now = [1000.0]
    s = make(tmp_path, clock=lambda: now[0])
    s.enqueue("k", "x", [("m1", "a")])
    s.mark_delivered("k", "x", "m1")
    now[0] += 31 * 86400
    s.prune(days=30)
    s.enqueue("k", "x", [("m1", "a")])
    assert s.next_queued("k", "x") == "m1"   # forgotten after 30 days (Graph ids of old mail won't reappear)
