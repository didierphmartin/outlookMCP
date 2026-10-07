import httpx
import pytest

from auth import Auth
from graph import GraphClient, TokenSource
from mail import MailService
from store import Store
from tests.conftest import NOW


def poll(svc, **args):
    return svc.poll_new_mail("key-1", {"folder": "Inbox", **args})


def test_old_mail_is_not_replayed(fake, svc):
    fake.add_message(subject="old", received="2026-10-07T09:00:00Z")
    assert poll(svc) == {"mail": None}


def test_one_per_call_oldest_first_never_twice(fake, svc):
    poll(svc)                                            # first use: cursor starts now
    fake.add_message(subject="b", received="2026-10-07T10:02:00Z")
    fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    fake.add_message(subject="c", received="2026-10-07T10:03:00Z")
    got = [poll(svc)["mail"]["subject"] for _ in range(3)]
    assert got == ["a", "b", "c"]
    assert poll(svc) == {"mail": None}


def test_mail_shape_and_attachments_never_opened(fake, svc):
    poll(svc)
    fake.add_message(subject="Invoice", sender="billing@acme.com", received="2026-10-07T10:01:00Z",
                     attachments=[("invoice.pdf", "application/pdf", b"%PDF-1.4 ...")],
                     body_html="<p>Hello <b>John</b></p><script>x()</script>")
    mail = poll(svc)["mail"]
    assert mail["from"] == "billing@acme.com" and mail["to"] == ["me@contoso.com"]
    assert mail["body_text"] == "Hello John"
    assert mail["web_link"].startswith("https://outlook.office.com/")
    assert mail["attachments"] == [{"id": mail["id"] + "-a0", "name": "invoice.pdf",
                                    "content_type": "application/pdf", "size": 12}]
    assert not [p for m, p in fake.requests if "/attachments/" in p], "a poll must never fetch attachment content"
    assert not [p for m, p in fake.requests if "contentBytes" in p]


def test_filters(fake, svc):
    poll(svc, **{"from": "acme.com"})
    fake.add_message(subject="x", sender="spam@evil.biz", received="2026-10-07T10:01:00Z")
    fake.add_message(subject="Invoice 4", sender="billing@ACME.com", received="2026-10-07T10:02:00Z")
    assert poll(svc, **{"from": "acme.com"})["mail"]["subject"] == "Invoice 4"
    assert poll(svc, **{"from": "acme.com"}) == {"mail": None}


def test_two_filter_sets_each_get_their_mail(fake, svc):
    poll(svc, subject_contains="invoice")
    poll(svc)
    fake.add_message(subject="Invoice 7", received="2026-10-07T10:01:00Z")
    assert poll(svc, subject_contains="invoice")["mail"]["subject"] == "Invoice 7"
    assert poll(svc)["mail"]["subject"] == "Invoice 7"


def test_paging_through_delta(fake, svc):
    poll(svc)
    for i in range(5):
        fake.add_message(subject=f"s{i}", received=f"2026-10-07T10:0{i + 1}:00Z")
    assert [poll(svc)["mail"]["subject"] for _ in range(5)] == [f"s{i}" for i in range(5)]


def test_message_deleted_before_fetch_is_skipped(fake, svc):
    poll(svc)
    a = fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    fake.add_message(subject="b", received="2026-10-07T10:02:00Z")
    svc._refresh_queue_for_tests("key-1", "Inbox", {})   # both queued
    del fake.messages[a]                                  # gone before fetch
    assert poll(svc)["mail"]["subject"] == "b"


def test_restart_keeps_queue_and_cursor(fake, http, cfg, svc):
    poll(svc)
    fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    fake.add_message(subject="b", received="2026-10-07T10:02:00Z")
    assert poll(svc)["mail"]["subject"] == "a"
    store2 = Store(cfg.db_path, cfg.secret_key)
    svc2 = MailService(lambda key: GraphClient(http, cfg.graph_base, TokenSource(Auth(http, cfg), store2, key)),
                       store2, cfg, now_iso=lambda: NOW)
    assert poll(svc2)["mail"]["subject"] == "b"
    assert poll(svc2) == {"mail": None}


def test_reconnect_needed(fake, svc):
    fake.revoke_refresh()
    assert poll(svc)["error"] == "reconnect_needed"


def test_unknown_folder(fake, svc):
    assert poll(svc, folder="Nope")["error"] == "unknown_folder"


# realistic delta ---------------------------------------------------------------------

def test_delta_pages_newest_first_still_deliver_oldest_first(fake, svc):
    poll(svc)
    for i in range(5):
        fake.add_message(subject=f"s{i}", received=f"2026-10-07T10:0{i + 1}:00Z")
    raw = fake._delta("inbox-id", {"$filter": [f"receivedDateTime ge {NOW}"]}).json()["value"]
    assert [m["subject"] for m in raw] == ["s4", "s3"], "the fake must not hand out oldest-first pages"
    assert [poll(svc)["mail"]["subject"] for _ in range(5)] == [f"s{i}" for i in range(5)]


def test_changed_message_is_never_delivered_twice(fake, svc):
    poll(svc)
    a = fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    assert poll(svc)["mail"]["subject"] == "a"
    fake.touch_message(a)                                 # read/flagged: delta sends it again
    assert poll(svc) == {"mail": None}


def test_touch_message_is_resent_by_delta(fake):
    a = fake.add_message(subject="a")
    link = fake._delta("inbox-id", {}).json()["@odata.deltaLink"]
    token = link.split("$deltatoken=")[1].split("&")[0]
    assert fake._delta("inbox-id", {"$deltatoken": [token]}).json()["value"] == []
    fake.touch_message(a)
    assert [m["id"] for m in fake._delta("inbox-id", {"$deltatoken": [token]}).json()["value"]] == [a]


def test_delta_link_keeps_the_received_filter(fake, svc):
    poll(svc)
    poll(svc)
    later = [p for _, p in fake.requests if "/messages/delta" in p][1:]
    assert later and all("deltatoken" in p and "receivedDateTime" in p for p in later)


def test_touched_old_mail_is_not_replayed(fake, svc):
    old = fake.add_message(subject="old", received="2026-10-07T09:00:00Z")
    poll(svc)
    fake.touch_message(old)
    assert poll(svc) == {"mail": None}


# folders ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [None, 404, 400])
def test_folder_by_display_name(fake, svc, status):
    fake.folder_name_status = status
    poll(svc, folder="Projects")
    fake.add_message(folder="projects-id", subject="p", received="2026-10-07T10:01:00Z")
    fake.add_message(subject="inbox mail", received="2026-10-07T10:02:00Z")
    assert poll(svc, folder="Projects")["mail"]["subject"] == "p"
    assert poll(svc, folder="Projects") == {"mail": None}


@pytest.mark.parametrize("status", [None, 404, 400])
def test_child_folder_path(fake, svc, status):
    fake.folder_name_status = status
    poll(svc, folder="Inbox/Invoices")
    fake.add_message(folder="invoices-id", subject="i", received="2026-10-07T10:01:00Z")
    assert poll(svc, folder="Inbox/Invoices")["mail"]["subject"] == "i"


# errors ----------------------------------------------------------------------------------

def make_svc(fake, cfg, http, store, slept=None):
    graph_for = lambda key: GraphClient(http, cfg.graph_base, TokenSource(Auth(http, cfg), store, key),
                                        sleep=(slept if slept is not None else []).append)
    return MailService(graph_for, store, cfg, now_iso=lambda: NOW)


@pytest.fixture
def store(fake, cfg):
    s = Store(cfg.db_path, cfg.secret_key)
    s.save_connection("key-1", fake.account, fake.tenant, fake._tokens_response().json()["refresh_token"])
    return s


def test_throttled_after_one_wait(fake, cfg, http, store):
    slept = []
    svc = make_svc(fake, cfg, http, store, slept)
    poll(svc)
    fake.throttle_next = 2
    assert poll(svc)["error"] == "throttled"
    assert slept == [2.0]


def test_transport_error_is_an_error_result(fake, cfg, store):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)
    svc = make_svc(fake, cfg, httpx.Client(transport=httpx.MockTransport(boom)), store)
    result = poll(svc)
    assert set(result) == {"error", "message"}


def test_graph_failure_on_fetch_keeps_the_mail_for_the_next_poll(fake, svc, monkeypatch):
    poll(svc)
    a = fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    real = fake._graph
    failed = []

    def flaky(path, q):
        if path == f"/me/messages/{a}" and not failed:
            failed.append(path)
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        return real(path, q)
    monkeypatch.setattr(fake, "_graph", flaky)
    assert poll(svc)["error"] == "graph_error"
    assert poll(svc)["mail"]["subject"] == "a"
    assert poll(svc) == {"mail": None}


def test_one_graph_client_per_connection(fake, cfg, http, store):
    built = []
    svc = MailService(lambda key: built.append(key) or GraphClient(
        http, cfg.graph_base, TokenSource(Auth(http, cfg), store, key)), store, cfg, now_iso=lambda: NOW)
    for _ in range(3):
        poll(svc)
    assert built == ["key-1"]
    token_calls = [p for _, p in fake.requests if p.endswith("/oauth2/v2.0/token")]
    assert len(token_calls) == 1


# fix round 1 -------------------------------------------------------------------------------

def test_refresh_transport_error_is_network_error_not_reconnect(fake, cfg, store):
    def handler(request):
        if request.url.path.endswith("/oauth2/v2.0/token"):
            raise httpx.ConnectError("dns failure", request=request)
        return fake.handler(request)
    svc = make_svc(fake, cfg, httpx.Client(transport=httpx.MockTransport(handler)), store)
    assert poll(svc)["error"] == "network_error"


def test_invalid_grant_is_still_reconnect_needed(fake, svc):
    fake.revoke_refresh()
    result = poll(svc)
    assert result["error"] == "reconnect_needed" and "invalid_grant" in result["message"]


def test_permanently_bad_message_is_reported_once_then_skipped(fake, svc, monkeypatch):
    poll(svc)
    a = fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    fake.add_message(subject="b", received="2026-10-07T10:02:00Z")
    real = fake._graph

    def broken(path, q):
        if path == f"/me/messages/{a}":
            return httpx.Response(400, json={"error": {"code": "ErrorInvalidRequest"}})
        return real(path, q)
    monkeypatch.setattr(fake, "_graph", broken)
    assert poll(svc)["error"] == "graph_error"
    assert poll(svc)["mail"]["subject"] == "b"
    assert poll(svc) == {"mail": None}


def test_unexpected_failure_is_internal_error(fake, svc, monkeypatch):
    poll(svc)
    fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    monkeypatch.setattr("mail.Message.shape", classmethod(lambda cls, m: 1 / 0))
    result = poll(svc)
    assert result["error"] == "internal_error" and "ZeroDivisionError" in result["message"]


@pytest.mark.parametrize("value,expected", [("false", False), ("0", False), ("", False), (None, False),
                                            (False, False), ("true", True), ("1", True), (True, True)])
def test_has_attachments_flag_parsing(value, expected):
    from mail import Filters
    assert Filters.from_args({"has_attachments": value}).has_attachments is expected


def test_body_text_survives_unclosed_head():
    from extract import html_to_text
    assert html_to_text("<html><head><style>p{}</style><body><p>Hi</p>") == "Hi"


def test_body_text_separates_table_cells():
    from extract import html_to_text
    assert html_to_text("<table><tr><td>Amount</td><td>42</td></tr></table>") == "Amount 42"


# fix round B7 ----------------------------------------------------------------------------

def _fetch_fails(fake, monkeypatch, mid, response, times):
    """The first `times` fetches of message `mid` answer `response()`; everything else is normal."""
    real, left = fake._graph, [times]

    def flaky(path, q):
        if path == f"/me/messages/{mid}" and left[0]:
            left[0] -= 1
            return response()
        return real(path, q)
    monkeypatch.setattr(fake, "_graph", flaky)


def test_reconnect_needed_on_fetch_keeps_the_mail(fake, svc, monkeypatch):
    poll(svc)
    a = fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    _fetch_fails(fake, monkeypatch, a, lambda: httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}}),
                 times=2)                                  # refused even after a refresh: reconnect_needed
    assert poll(svc)["error"] == "reconnect_needed"
    assert poll(svc)["mail"]["subject"] == "a"           # the user reconnected: the mail was not lost
    assert poll(svc) == {"mail": None}


def test_non_json_fetch_body_keeps_the_mail(fake, svc, monkeypatch):
    poll(svc)
    a = fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    _fetch_fails(fake, monkeypatch, a, lambda: httpx.Response(200, content=b"<html>gateway</html>"), times=1)
    assert poll(svc)["error"] == "graph_error"
    assert poll(svc)["mail"]["subject"] == "a"
    assert poll(svc) == {"mail": None}


# final review fixes ------------------------------------------------------------------------

@pytest.mark.parametrize("status", [410, 400])
def test_stale_delta_link_restarts_delta_without_redelivery(fake, svc, status):
    poll(svc)
    fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    fake.add_message(subject="b", received="2026-10-07T10:02:00Z")
    assert poll(svc)["mail"]["subject"] == "a"
    assert poll(svc)["mail"]["subject"] == "b"
    fake.delta_link_failure = status                     # the stored delta link is no longer valid
    fake.add_message(subject="c", received="2026-10-07T10:03:00Z")
    before = len(fake.requests)
    assert poll(svc)["mail"]["subject"] == "c"
    assert poll(svc) == {"mail": None}                   # a and b are not delivered again
    restarted = [p for _, p in fake.requests[before:] if "/messages/delta" in p and "deltatoken" not in p]
    assert len(restarted) == 1 and "receivedDateTime" in restarted[0] and "10%3A00%3A00Z" in restarted[0]
    fake.add_message(subject="d", received="2026-10-07T10:04:00Z")
    assert poll(svc)["mail"]["subject"] == "d"           # the new delta link works


def test_failed_first_delta_does_not_move_now_forward(fake, cfg, http, store, monkeypatch):
    clock = ["2026-10-07T10:00:00Z"]
    svc = MailService(lambda key: GraphClient(http, cfg.graph_base, TokenSource(Auth(http, cfg), store, key)),
                      store, cfg, now_iso=lambda: clock[0])
    real, failed = fake._graph, []

    def flaky(path, q):
        if path.endswith("/messages/delta") and not failed:
            failed.append(path)
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        return real(path, q)
    monkeypatch.setattr(fake, "_graph", flaky)
    assert poll(svc)["error"] == "graph_error"
    fake.add_message(subject="between", received="2026-10-07T10:05:00Z")
    clock[0] = "2026-10-07T10:10:00Z"                    # the first delta failed; time moved on
    assert poll(svc)["mail"]["subject"] == "between"


def test_changed_secret_key_is_reconnect_needed(fake, cfg, http, store):
    from config import AppConfig
    other = Store(cfg.db_path, AppConfig.new_secret_key())    # OUTLOOK_SECRET_KEY changed
    svc = make_svc(fake, cfg, http, other)
    assert poll(svc)["error"] == "reconnect_needed"
    assert svc.whoami("key-1")["error"] == "reconnect_needed"


def test_every_graph_request_asks_for_immutable_ids(fake, svc):
    poll(svc)
    fake.add_message(subject="a", received="2026-10-07T10:01:00Z")
    poll(svc)
    graph = [h for (_, p), h in zip(fake.requests, fake.headers) if "/oauth2/" not in p]
    assert graph and all(h.get("prefer") == 'IdType="ImmutableId"' for h in graph)
