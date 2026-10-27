"""The write tools: move_mail files an email into a folder, forward_mail sends it on to other addresses."""
import pytest

from graph import GraphError


def posted(mock, suffix):
    return [body for path, body in mock.posted if path.endswith(suffix)]


def test_move_to_existing_folder(mock, svc):
    mid = mock.add_message(subject="inv")
    r = svc.move_mail("key-1", {"mail_id": mid, "folder": "Inbox/Invoices"})
    assert r == {"id": mid, "folder": "Inbox/Invoices", "created": []}
    assert mock.messages[mid]["folder"] == "invoices-id"
    assert posted(mock, f"/messages/{mid}/move") == [{"destinationId": "invoices-id"}]


def test_move_keeps_the_id_so_later_steps_still_work(mock, svc):
    mid = mock.add_message(attachments=[("note.txt", "text/plain", b"hi")])
    svc.move_mail("key-1", {"mail_id": mid, "folder": "Projects"})
    assert svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a0"})["text"] == "hi"


def test_move_creates_missing_folders_along_the_path(mock, svc):
    mid = mock.add_message()
    r = svc.move_mail("key-1", {"mail_id": mid, "folder": "Dispatched/Sales"})
    assert r["created"] == ["Dispatched", "Dispatched/Sales"]
    paths = {f["path"] for f in svc.list_folders("key-1")["folders"]}
    assert {"Dispatched", "Dispatched/Sales"} <= paths
    assert mock.folder(mock.messages[mid]["folder"])["displayName"] == "Sales"
    # a second move reuses them
    mid2 = mock.add_message()
    assert svc.move_mail("key-1", {"mail_id": mid2, "folder": "dispatched/sales"})["created"] == []


def test_move_creates_a_subfolder_under_a_well_known_folder(mock, svc):
    mid = mock.add_message()
    assert svc.move_mail("key-1", {"mail_id": mid, "folder": "Inbox/Done"})["created"] == ["Inbox/Done"]
    assert mock.folder(mock.messages[mid]["folder"])["parent"] == "inbox-id"


def test_moved_mail_leaves_the_source_folder_for_polling(mock, svc):
    svc.poll_new_mail("key-1", {})                                   # sets the cursor
    mid = mock.add_message(received="2026-10-07T10:01:00Z")
    assert svc.poll_new_mail("key-1", {})["mail"]["id"] == mid
    svc.move_mail("key-1", {"mail_id": mid, "folder": "Projects"})
    assert svc.poll_new_mail("key-1", {})["mail"] is None


@pytest.mark.parametrize("args, code", [
    ({"folder": "Projects"}, "invalid_argument"),
    ({"mail_id": "m1"}, "invalid_argument"),
    ({"mail_id": "zz", "folder": "Projects"}, "not_found"),
])
def test_move_errors(mock, svc, args, code):
    mock.add_message()
    assert svc.move_mail("key-1", args)["error"] == code


def test_forward_to_one_or_more_addresses(mock, svc):
    mid = mock.add_message(subject="lead")
    r = svc.forward_mail("key-1", {"mail_id": mid, "to": "sales@contoso.com; Boss@Contoso.com", "comment": "New lead"})
    assert r == {"id": mid, "forwarded_to": ["sales@contoso.com", "Boss@Contoso.com"]}
    assert posted(mock, f"/messages/{mid}/forward") == [{
        "comment": "New lead",
        "toRecipients": [{"emailAddress": {"address": "sales@contoso.com"}},
                         {"emailAddress": {"address": "Boss@Contoso.com"}}]}]


def test_forward_accepts_a_list_and_no_comment(mock, svc):
    mid = mock.add_message()
    assert svc.forward_mail("key-1", {"mail_id": mid, "to": ["a@x.com", "b@y.com"]})["forwarded_to"] == ["a@x.com", "b@y.com"]
    assert posted(mock, "/forward")[0]["comment"] == ""


@pytest.mark.parametrize("to", ["", "not-an-address", [], "a@x.com, nope"])
def test_forward_rejects_bad_recipients_before_calling_graph(mock, svc, to):
    mid = mock.add_message()
    assert svc.forward_mail("key-1", {"mail_id": mid, "to": to})["error"] == "invalid_argument"
    assert not posted(mock, "/forward")


def test_missing_permission_is_reported_as_forbidden(mock, svc):
    """A connection signed in before the write permissions were granted: Graph answers 403."""
    mid = mock.add_message()
    mock.forbid_writes = True
    r = svc.move_mail("key-1", {"mail_id": mid, "folder": "Projects"})
    assert r["error"] == "forbidden" and "Mail.ReadWrite" in r["message"]
    assert svc.forward_mail("key-1", {"mail_id": mid, "to": "a@x.com"})["error"] == "forbidden"


def test_graph_post_returns_empty_dict_for_202(mock, http, cfg):
    from graph import GraphClient, StaticToken
    token = mock._tokens_response().json()["access_token"]
    mid = mock.add_message()
    g = GraphClient(http, cfg.graph_base, StaticToken(token))
    assert g.post(f"/me/messages/{mid}/forward", {"toRecipients": [], "comment": ""}) == {}
    with pytest.raises(GraphError) as e:
        g.post("/me/messages/zz/move", {"destinationId": "projects-id"})
    assert e.value.code == "not_found"


def test_write_tools_over_mcp(mock, http, cfg):
    from tests.test_server import app, call, connect   # the protocol helpers the server tests use
    c = app(mock, http, cfg)
    key = connect(c, mock)
    mid = mock.add_message()
    assert call(c, "move_mail", {"mail_id": mid, "folder": "Projects"}, key=key)["folder"] == "Projects"
    assert call(c, "forward_mail", {"mail_id": mid, "to": "a@x.com"}, key=key)["forwarded_to"] == ["a@x.com"]
    assert call(c, "move_mail", {"mail_id": mid, "folder": "Projects"})["error"] == "not_connected"


def test_scopes_ask_for_write_and_send():
    from config import AppConfig
    assert {"Mail.ReadWrite", "Mail.Send"} <= set(AppConfig.SCOPES.split())
