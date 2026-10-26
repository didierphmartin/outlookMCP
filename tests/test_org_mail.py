"""An organization connection: poll_new_mail and the mail tools across the group's mailboxes."""
import sqlite3

import httpx
import pytest

from mailboxes import Mailbox, MailboxDirectory
from tests.test_org_connect import org_cfg  # noqa: F401 -- the fixture
from tests.test_server import app, call, connect

DROP = {"folder": "To SharePoint", "mode": "drop"}


@pytest.fixture
def org(fake, http, org_cfg):  # noqa: F811
    fake.add_mailbox("u-alice", "alice@contoso.com", "Alice Martin")
    fake.add_mailbox("u-bob", "bob@contoso.com", "Bob Stone")
    c = app(fake, http, org_cfg)
    s = call(c, "connect_start", {"method": "organization"})
    return c, call(c, "connect_status", {"connect_id": s["connect_id"]})["connection_key"]


def later(c, seconds=901):
    """Move the server's clock past the group refresh interval."""
    c.app.state.clock_offset[0] += seconds


def drop_folder(fake):
    return next(f["id"] for f in fake.folders if f["displayName"] == "To SharePoint")


def rows(cfg, table):
    return sqlite3.connect(cfg.db_path).execute(f"SELECT scope FROM {table}").fetchall()


def test_drops_from_two_mailboxes_come_oldest_first(fake, org):
    c, key = org
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}       # first watch: creates, baseline
    a = fake.add_message(folder="To SharePoint", subject="alice", received="2026-10-07T10:05:00Z", mailbox="u-alice")
    b = fake.add_message(folder="To SharePoint", subject="bob", received="2026-10-07T10:01:00Z", mailbox="u-bob")
    assert call(c, "poll_new_mail", DROP, key=key)["mail"]["id"] == f"u-bob~{b}"
    assert call(c, "poll_new_mail", DROP, key=key)["mail"]["id"] == f"u-alice~{a}"
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}


def test_mail_carries_mailbox_and_name(fake, org):
    c, key = org
    call(c, "poll_new_mail", DROP, key=key)
    fake.add_message(folder="To SharePoint", subject="hi", mailbox="u-bob")
    mail = call(c, "poll_new_mail", DROP, key=key)["mail"]
    assert mail["mailbox"] == "bob@contoso.com" and mail["mailbox_name"] == "Bob Stone"
    assert mail["to"] == ["bob@contoso.com"] and mail["attempt"] == 1
    assert any(m == "GET" and p.startswith("/v1.0/users/u-bob/mailFolders") for m, p in fake.requests)


def test_move_mail_acts_on_the_right_mailbox(fake, org):
    c, key = org
    call(c, "poll_new_mail", DROP, key=key)
    a = fake.add_message(folder="To SharePoint", subject="alice", received="2026-10-07T10:05:00Z", mailbox="u-alice")
    b = fake.add_message(folder="To SharePoint", subject="bob", received="2026-10-07T10:01:00Z", mailbox="u-bob")
    mail = call(c, "poll_new_mail", DROP, key=key)["mail"]
    r = call(c, "move_mail", {"mail_id": mail["id"], "folder": "Filed"}, key=key)
    assert r == {"id": f"u-bob~{b}", "folder": "Filed", "created": ["Filed"]}
    filed = next(f["id"] for f in fake.folders if f["displayName"] == "Filed")
    assert fake.messages[b]["folder"] == filed and fake.messages[b]["mailbox"] == "u-bob"
    assert fake.messages[a]["folder"] == drop_folder(fake)
    assert ("POST", f"/v1.0/users/u-bob/messages/{b}/move") in fake.requests


def test_forward_mail_resolves_opaque_id(fake, org):
    c, key = org
    call(c, "poll_new_mail", DROP, key=key)
    b = fake.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob")
    mail = call(c, "poll_new_mail", DROP, key=key)["mail"]
    assert call(c, "forward_mail", {"mail_id": mail["id"], "to": "x@y.com"}, key=key) == \
        {"id": mail["id"], "forwarded_to": ["x@y.com"]}
    assert ("POST", f"/v1.0/users/u-bob/messages/{b}/forward") in fake.requests


def test_get_attachment_and_mail_to_pdf_resolve_opaque_id(fake, org):
    c, key = org
    call(c, "poll_new_mail", DROP, key=key)
    b = fake.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob",
                         attachments=[("note.txt", "text/plain", b"hello from bob")])
    mail = call(c, "poll_new_mail", DROP, key=key)["mail"]
    att = call(c, "get_attachment", {"mail_id": mail["id"], "attachment_id": mail["attachments"][0]["id"]}, key=key)
    assert att["text"].strip() == "hello from bob"
    pdf = call(c, "mail_to_pdf", {"mail_id": mail["id"]}, key=key)
    assert pdf["name"] == "Email.pdf" and pdf["size"] > 0
    assert any(p.startswith(f"/v1.0/users/u-bob/messages/{b}/attachments/") for _, p in fake.requests)
    assert not any(p.startswith(f"/v1.0/users/u-alice/messages/{b}") for _, p in fake.requests)


def test_forbidden_mailbox_skipped_others_delivered(fake, org, caplog):
    c, key = org
    call(c, "poll_new_mail", DROP, key=key)
    fake.add_message(folder="To SharePoint", subject="alice", received="2026-10-07T10:01:00Z", mailbox="u-alice")
    b = fake.add_message(folder="To SharePoint", subject="bob", received="2026-10-07T10:05:00Z", mailbox="u-bob")
    fake.forbid_mailbox("u-alice")                         # licence gone / Exchange scope: 403 mid-cycle
    with caplog.at_level("WARNING", logger="outlook_mcp"):
        assert call(c, "poll_new_mail", DROP, key=key)["mail"]["id"] == f"u-bob~{b}"
    assert "u-alice" in caplog.text and "forbidden" in caplog.text
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}


def test_forbidden_mailbox_on_the_first_watch_does_not_fail_it(fake, org):
    c, key = org
    fake.forbid_mailbox("u-alice")
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}
    b = fake.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob")
    assert call(c, "poll_new_mail", DROP, key=key)["mail"]["id"] == f"u-bob~{b}"
    fake.forbid_mailbox("u-bob")                                           # every member out of reach: an error
    assert call(c, "poll_new_mail", DROP, key=key)["error"] == "forbidden"


def test_one_broken_mailbox_never_stops_the_others(fake, org):
    """Alice is throttled (429 twice), Bob has no "Projects" folder (unknown_folder): Carol's mail still comes."""
    c, key = org
    fake.add_mailbox("u-carol", "carol@contoso.com", "Carol")
    select = fake._select_mailbox

    def broken(path):
        if path.startswith("/users/u-alice/"):
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"code": "TooManyRequests"}})
        if path.startswith("/users/u-bob/mailFolders"):
            return httpx.Response(200, json={"value": []}) if path.endswith("/mailFolders") \
                else httpx.Response(404, json={"error": {"code": "ErrorFolderNotFound"}})
        return select(path)
    fake._select_mailbox = broken
    new = {"folder": "Projects"}
    assert call(c, "poll_new_mail", new, key=key) == {"mail": None}
    m = fake.add_message(folder="projects-id", subject="carol", received="2099-01-01T00:00:00Z", mailbox="u-carol")
    fake.add_message(folder="projects-id", subject="alice", received="2099-01-01T00:00:00Z", mailbox="u-alice")
    assert call(c, "poll_new_mail", new, key=key)["mail"]["id"] == f"u-carol~{m}"
    assert call(c, "poll_new_mail", new, key=key) == {"mail": None}


def test_baseline_until_a_first_round_reaches_every_mailbox(fake, org):
    c, key = org
    fake.folders.append({"id": "drop-id", "displayName": "To SharePoint"})
    fake.add_message(folder="drop-id", subject="alice's old", mailbox="u-alice")
    fake.add_message(folder="drop-id", subject="bob's old", mailbox="u-bob")
    fake.forbid_mailbox("u-bob")
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}       # Alice baselined, Bob skipped
    fake.forbidden_mailboxes.discard("u-bob")
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}       # no complete round yet: Bob baselined
    fake.add_mailbox("u-carol", "carol@contoso.com", "Carol")
    carol = fake.add_message(folder="drop-id", subject="carol's old", mailbox="u-carol")
    later(c)
    assert call(c, "poll_new_mail", DROP, key=key)["mail"]["id"] == f"u-carol~{carol}"
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}


def test_opaque_id_of_removed_mailbox_is_an_error_result(fake, org):
    c, key = org
    call(c, "poll_new_mail", DROP, key=key)
    b = fake.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob")
    mail = call(c, "poll_new_mail", DROP, key=key)["mail"]
    fake.mailboxes["u-bob"]["in_group"] = False
    later(c)
    seen = len(fake.requests)
    for tool, extra in (("move_mail", {"folder": "Filed"}), ("forward_mail", {"to": "x@y.com"}),
                        ("get_attachment", {"attachment_id": "a"}), ("mail_to_pdf", {})):
        r = call(c, tool, {"mail_id": mail["id"], **extra}, key=key)
        assert r["error"] == "forbidden", (tool, r)
    assert fake.messages[b]["folder"] == drop_folder(fake)
    assert not any("/users/u-bob/" in p for _, p in fake.requests[seen:])
    assert call(c, "move_mail", {"mail_id": b, "folder": "Filed"}, key=key)["error"] == "invalid_argument"   # no mailbox part


def test_same_display_names_get_distinct_safe_names():
    graph = StubGraph([{"id": "1", "mail": "alice@contoso.com", "displayName": "Alice Martin"},
                       {"id": "2", "mail": "alice.m@contoso.com", "displayName": "alice martin"},
                       {"id": "3", "mail": "bob@contoso.com", "displayName": "Bob"}])
    assert MailboxDirectory(graph, "g", clock=lambda: 0).members() == [
        Mailbox("1", "alice@contoso.com", "Alice Martin (alice@contoso.com)"),
        Mailbox("2", "alice.m@contoso.com", "alice martin (alice.m@contoso.com)"),
        Mailbox("3", "bob@contoso.com", "Bob")]


def test_unsafe_characters_replaced():
    safe = MailboxDirectory.safe_name
    assert safe('A/B\\C:D*E?F"G<H>I|J') == "A-B-C-D-E-F-G-H-I-J"
    assert safe(" ..Bob Stone.. ") == "Bob Stone"
    assert safe(" . ", "x@contoso.com") == "x@contoso.com"
    graph = StubGraph([{"id": "1", "mail": "a@contoso.com", "displayName": "..."},
                       {"id": "2", "mail": None, "displayName": "No Mailbox"},
                       {"id": "3", "mail": "c@contoso.com", "displayName": "R&D: Lab"}])
    assert [m.name for m in MailboxDirectory(graph, "g", clock=lambda: 0).members()] == ["a@contoso.com", "R&D- Lab"]


def test_directory_rereads_after_refresh_and_reports_departed():
    t = [0.0]
    graph = StubGraph([{"id": "1", "mail": "a@c.com", "displayName": "A"}, {"id": "2", "mail": "b@c.com", "displayName": "B"}],
                      page_size=1)
    d = MailboxDirectory(graph, "g", clock=lambda: t[0], refresh_s=900)
    gone = []
    d.on_departed(gone.extend)
    assert [m.id for m in d.members()] == ["1", "2"]                       # both pages read
    graph.members = graph.members[:1]
    t[0] = 899
    assert [m.id for m in d.members()] == ["1", "2"] and gone == []        # still fresh: not re-read
    t[0] = 901
    assert [m.id for m in d.members()] == ["1"]
    assert gone == [Mailbox("2", "b@c.com", "B")]
    assert graph.paths[0] == ("/groups/g/members/microsoft.graph.user",
                              {"$select": "id,mail,displayName"})


def test_first_watch_is_baseline_later_mailbox_is_not(fake, org):
    c, key = org
    fake.folders.append({"id": "drop-id", "displayName": "To SharePoint"})
    fake.add_message(folder="drop-id", subject="already there", mailbox="u-alice")
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}       # the baseline: never delivered
    fake.add_mailbox("u-carol", "carol@contoso.com", "Carol")
    carol = fake.add_message(folder="drop-id", subject="prepared before joining", mailbox="u-carol")
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}       # the group is not re-read yet
    later(c)
    assert call(c, "poll_new_mail", DROP, key=key)["mail"]["id"] == f"u-carol~{carol}"
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}


def test_left_group_rows_purged(fake, org, org_cfg):  # noqa: F811
    c, key = org
    call(c, "poll_new_mail", DROP, key=key)
    fake.add_message(folder="To SharePoint", subject="bob", mailbox="u-bob")
    call(c, "poll_new_mail", {**DROP, "folder": "Inbox"}, key=key)          # queues nothing, but makes scopes
    assert any(s.startswith("u-bob|") for (s,) in rows(org_cfg, "cursors"))
    fake.mailboxes["u-bob"]["in_group"] = False
    later(c)
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}       # bob's email is not delivered
    assert not any(s.startswith("u-bob|") for (s,) in rows(org_cfg, "queue") + rows(org_cfg, "cursors"))
    assert any(s.startswith("u-alice|") for (s,) in rows(org_cfg, "cursors"))


def test_rejoined_mailbox_gets_no_baseline(fake, org):
    c, key = org
    call(c, "poll_new_mail", DROP, key=key)
    fake.mailboxes["u-bob"]["in_group"] = False
    later(c)
    call(c, "poll_new_mail", DROP, key=key)
    b = fake.add_message(folder="To SharePoint", subject="dropped while out", mailbox="u-bob")
    fake.mailboxes["u-bob"]["in_group"] = True
    later(c)
    assert call(c, "poll_new_mail", DROP, key=key)["mail"]["id"] == f"u-bob~{b}"


def test_list_folders_needs_mailbox(fake, org):
    c, key = org
    assert call(c, "list_folders", {}, key=key)["error"] == "invalid_argument"
    assert call(c, "list_folders", {"mailbox": "eve@contoso.com"}, key=key)["error"] == "invalid_argument"
    for who in ("bob@contoso.com", "BOB@contoso.com", "u-bob"):
        assert "Inbox" in [f["path"] for f in call(c, "list_folders", {"mailbox": who}, key=key)["folders"]]
    assert any(p.startswith("/v1.0/users/u-bob/mailFolders") for _, p in fake.requests)


def test_whoami_names_the_group_and_counts_mailboxes(fake, org):
    c, key = org
    r = call(c, "whoami", {}, key=key)
    assert r["account"] == "VIP Mailboxes" and r["tenant"] == "tid-1" and r["mailboxes"] == 2
    assert isinstance(r["connected_at"], float)


def test_user_connection_ids_and_scopes_unchanged(fake, http, cfg):
    c = app(fake, http, cfg)
    key = connect(c, fake)
    assert call(c, "poll_new_mail", DROP, key=key) == {"mail": None}
    mid = fake.add_message(folder="To SharePoint", subject="mine")
    mail = call(c, "poll_new_mail", DROP, key=key)["mail"]
    assert mail["id"] == mid and "mailbox" not in mail and "mailbox_name" not in mail
    folder = drop_folder(fake)
    (scope,), = rows(cfg, "cursors")
    assert scope.startswith(f"{folder}|") and scope.count("|") == 1
    # A later drop folder of a user connection is still a baseline of its own (unchanged rule).
    fake.add_message(folder="projects-id", subject="already there")
    assert call(c, "poll_new_mail", {"folder": "Projects", "mode": "drop"}, key=key) == {"mail": None}
    assert call(c, "move_mail", {"mail_id": mid, "folder": "Filed"}, key=key)["id"] == mid
    assert all("/users/" not in p for _, p in fake.requests)
    assert "mailboxes" not in call(c, "whoami", {}, key=key)


class StubGraph:
    """GraphClient.get for the group members only, paged like Graph."""

    def __init__(self, members, page_size=100):
        self.members, self.page_size, self.paths = members, page_size, []

    def get(self, path, params=None):
        self.paths.append((path, params))
        skip = int(path.rsplit("skip=", 1)[1]) if "skip=" in path else 0
        body = {"value": self.members[skip:skip + self.page_size]}
        if skip + self.page_size < len(self.members):
            body["@odata.nextLink"] = f"https://graph/next?skip={skip + self.page_size}"
        return body
