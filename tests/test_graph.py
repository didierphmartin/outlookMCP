import pytest

from auth import Auth
from graph import GraphClient, GraphError, StaticToken, TokenSource
from store import Store


def connected(mock, http, cfg):
    """A stored connection whose refresh token the mock knows."""
    tokens = mock._tokens_response().json()
    store = Store(cfg.db_path, cfg.secret_key)
    store.save_connection("key-1", mock.account, mock.tenant, tokens["refresh_token"])
    return store, TokenSource(Auth(http, cfg), store, "key-1")


def test_get_with_static_token(mock, http, cfg):
    token = mock._tokens_response().json()["access_token"]
    me = GraphClient(http, cfg.graph_base, StaticToken(token)).get("/me")
    assert me["mail"] == "me@contoso.com"


def test_token_source_refreshes_and_rotates(mock, http, cfg):
    store, ts = connected(mock, http, cfg)
    old_rt = store.connection("key-1")["refresh_token"]
    g = GraphClient(http, cfg.graph_base, ts)
    assert g.get("/me")["mail"] == "me@contoso.com"
    assert store.connection("key-1")["refresh_token"] != old_rt   # rotated token saved


def test_401_refreshes_once(mock, http, cfg):
    _, ts = connected(mock, http, cfg)
    g = GraphClient(http, cfg.graph_base, ts)
    g.get("/me")
    mock.expire_access_tokens()
    assert g.get("/me")["mail"] == "me@contoso.com"


def test_revoked_refresh_is_reconnect_needed(mock, http, cfg):
    _, ts = connected(mock, http, cfg)
    mock.revoke_refresh()
    with pytest.raises(GraphError) as e:
        GraphClient(http, cfg.graph_base, ts).get("/me")
    assert e.value.code == "reconnect_needed"


def test_unknown_connection_is_reconnect_needed(mock, http, cfg):
    ts = TokenSource(Auth(http, cfg), Store(cfg.db_path, cfg.secret_key), "never-connected")
    with pytest.raises(GraphError, match="reconnect_needed"):
        GraphClient(http, cfg.graph_base, ts).get("/me")


def test_429_waits_once_then_throttled(mock, http, cfg):
    token = mock._tokens_response().json()["access_token"]
    slept = []
    g = GraphClient(http, cfg.graph_base, StaticToken(token), sleep=slept.append)
    mock.throttle_next = 1
    assert g.get("/me")["mail"]
    assert slept == [2.0]
    mock.throttle_next = 2
    with pytest.raises(GraphError, match="throttled"):
        g.get("/me")


@pytest.mark.parametrize("status,code", [(404, "not_found"), (400, "graph_error")])
def test_mock_folder_by_name_can_refuse(mock, http, cfg, status, code):
    token = mock._tokens_response().json()["access_token"]
    g = GraphClient(http, cfg.graph_base, StaticToken(token))
    assert g.get("/me/mailFolders/Invoices")["id"] == "invoices-id"   # default: resolved
    mock.folder_name_status = status
    with pytest.raises(GraphError) as e:
        g.get("/me/mailFolders/Invoices")
    assert e.value.code == code
    assert g.get("/me/mailFolders/inbox")["id"] == "inbox-id"          # well-known names still resolve
