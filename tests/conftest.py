import httpx
import pytest

from config import AppConfig
from fake_graph import FakeGraph


@pytest.fixture
def fake():
    return FakeGraph()


@pytest.fixture
def http(fake):
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(client_id="cid", secret_key=AppConfig.new_secret_key(), db_path=str(tmp_path / "o.sqlite"),
                     public_url="https://synergy.example.com/outlook")


NOW = "2026-10-07T10:00:00Z"   # the mail service's "now" in tests: messages at 10:01+ are new


@pytest.fixture
def svc(fake, http, cfg):
    """A MailService over FakeGraph with one stored connection, "key-1". (mail.py arrives in Task B5.)"""
    from auth import Auth
    from graph import GraphClient, TokenSource
    from mail import MailService
    from store import Store
    store = Store(cfg.db_path, cfg.secret_key)
    store.save_connection("key-1", fake.account, fake.tenant, fake._tokens_response().json()["refresh_token"])
    graph_for = lambda key: GraphClient(http, cfg.graph_base, TokenSource(Auth(http, cfg), store, key))
    return MailService(graph_for, store, cfg, now_iso=lambda: NOW)
