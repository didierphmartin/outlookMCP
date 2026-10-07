from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from auth import Auth, AuthError, Connector
from store import Store


def connector(fake, http, cfg, clock=None):
    t = clock or [1000.0]
    return Connector(Auth(http, cfg), Store(cfg.db_path, cfg.secret_key), http, cfg, clock=lambda: t[0]), t


def test_device_flow_end_to_end(fake, http, cfg):
    c, t = connector(fake, http, cfg)
    s = c.start()
    assert s["user_code"] == "K7PQ-M2XD" and s["verification_uri"] == "https://microsoft.com/devicelogin"
    assert c.status(s["connect_id"])["status"] == "pending"
    fake.device_state = "connected"
    t[0] += 6
    done = c.status(s["connect_id"])
    assert done["status"] == "connected" and done["account"] == "me@contoso.com"
    key = done["connection_key"]
    assert len(key) >= 32
    again = c.status(s["connect_id"])
    assert again["status"] == "connected" and "connection_key" not in again   # handed out once
    assert Store(cfg.db_path, cfg.secret_key).connection(key)["tenant"] == "tid-1"


def test_status_respects_poll_interval(fake, http, cfg):
    c, t = connector(fake, http, cfg)
    s = c.start()
    c.status(s["connect_id"])
    polls = sum(1 for m, p in fake.requests if p.endswith("/token"))
    t[0] += 1                                   # app polls every 3 s, Microsoft's interval is 5 s
    c.status(s["connect_id"])
    assert sum(1 for m, p in fake.requests if p.endswith("/token")) == polls


def test_expired_and_declined(fake, http, cfg):
    c, t = connector(fake, http, cfg)
    s = c.start()
    t[0] += 901
    assert c.status(s["connect_id"])["status"] == "expired"
    s2 = c.start()
    fake.device_state = "declined"
    t[0] += 6
    assert c.status(s2["connect_id"])["status"] == "denied"
    assert c.status("nope")["status"] == "unknown"


def test_policy_block_offers_redirect(fake, http, cfg):
    c, t = connector(fake, http, cfg)
    s = c.start()
    fake.device_state = "blocked"
    t[0] += 6
    st = c.status(s["connect_id"])
    assert st["status"] == "policy_blocked"
    assert st["redirect_url"] == f"https://synergy.example.com/outlook/auth/start?connect_id={s['connect_id']}"


def test_redirect_fallback(fake, http, cfg):
    c, t = connector(fake, http, cfg)
    s = c.start()
    url = urlparse(c.redirect_start(s["connect_id"]))
    q = parse_qs(url.query)
    assert url.path.endswith("/oauth2/v2.0/authorize")
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["https://synergy.example.com/outlook/auth/callback"]
    assert "Connected" in c.redirect_callback(q["state"][0], "auth-code-1")
    done = c.status(s["connect_id"])
    assert done["status"] == "connected" and done["connection_key"]


def test_redirect_needs_public_url(fake, http, cfg):
    c, t = connector(fake, http, replace(cfg, public_url=""))
    s = c.start()
    fake.device_state = "blocked"
    t[0] += 6
    st = c.status(s["connect_id"])
    assert st["status"] == "policy_blocked" and st["redirect_url"] is None and "OUTLOOK_PUBLIC_URL" in st["message"]


# --- client secret: optional, sent only when configured ---------------------------------------------


def _token_forms(fake_http_log):
    return [parse_qs(r.content.decode()) for r in fake_http_log if r.url.path.endswith("/token")]


def _logging_http(fake):
    log = []

    def handler(request):
        log.append(request)
        return fake.handler(request)
    return httpx.Client(transport=httpx.MockTransport(handler)), log


@pytest.mark.parametrize("secret", ["", "s3cret"])
def test_client_secret_sent_only_when_configured(fake, cfg, secret):
    http, log = _logging_http(fake)
    cfg = replace(cfg, client_secret=secret)
    c, t = connector(fake, http, cfg)
    s = c.start()
    fake.device_state = "connected"
    c.status(s["connect_id"])                                   # device-code polling
    s2 = c.start()
    q = parse_qs(urlparse(c.redirect_start(s2["connect_id"])).query)
    c.redirect_callback(q["state"][0], "auth-code-1")           # PKCE code exchange
    rt = next(iter(fake.refresh_tokens))
    Auth(http, cfg).refresh(rt)                                 # refresh
    forms = _token_forms(log)
    grants = [f["grant_type"][0] for f in forms]
    assert grants == ["urn:ietf:params:oauth:grant-type:device_code", "authorization_code", "refresh_token"]
    for f in forms:
        if secret:
            assert f["client_secret"] == [secret]
        else:
            assert "client_secret" not in f


# --- identity endpoint failures become AuthError ----------------------------------------------------

def _auth_with(handler, cfg):
    return Auth(httpx.Client(transport=httpx.MockTransport(handler)), cfg)


def test_html_gateway_error_becomes_auth_error(cfg):
    a = _auth_with(lambda r: httpx.Response(502, text="<html><body>Bad Gateway</body></html>"), cfg)
    for call in (a.device_start, lambda: a.refresh("rt"), lambda: a.redeem_code("c", "v", "https://x/cb")):
        with pytest.raises(AuthError) as e:
            call()
        assert "502" in str(e.value) and len(str(e.value)) < 200


def test_transport_error_becomes_auth_error(cfg):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)
    a = _auth_with(boom, cfg)
    with pytest.raises(AuthError):
        a.device_start()
    with pytest.raises(AuthError):
        a.refresh("rt")


def test_timeout_during_device_poll_is_pending(cfg):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)
    assert _auth_with(slow, cfg).device_poll("dc-1") == ("pending", None)


def test_gateway_502_and_temporarily_unavailable_during_device_poll_are_pending(cfg):
    html = _auth_with(lambda r: httpx.Response(502, text="<html>Bad Gateway</html>"), cfg)
    assert html.device_poll("dc-1") == ("pending", None)
    busy = _auth_with(lambda r: httpx.Response(503, json={"error": "temporarily_unavailable"}), cfg)
    assert busy.device_poll("dc-1") == ("pending", None)


def test_transient_error_keeps_connect_alive(fake, cfg):
    calls = [0]

    def flaky(request):
        if request.url.path.endswith("/token") and calls[0] == 0:
            calls[0] += 1
            return httpx.Response(502, text="<html>Bad Gateway</html>")
        return fake.handler(request)
    http = httpx.Client(transport=httpx.MockTransport(flaky))
    c, t = connector(fake, http, cfg)
    s = c.start()
    assert c.status(s["connect_id"])["status"] == "pending"
    fake.device_state = "connected"
    t[0] += 6
    assert c.status(s["connect_id"])["status"] == "connected"


def test_terminal_failure_reports_reason(fake, cfg):
    def bad_client(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(400, json={"error": "invalid_client",
                                             "error_description": "AADSTS7000218: client_assertion or client_secret required."})
        return fake.handler(request)
    http = httpx.Client(transport=httpx.MockTransport(bad_client))
    c, t = connector(fake, http, replace(cfg, client_secret="s3cret"))
    s = c.start()
    st = c.status(s["connect_id"])
    assert st["status"] == "failed" and "AADSTS7000218" in st["message"] and "s3cret" not in st["message"]
    again = c.status(s["connect_id"])                       # the reason survives on later polls
    assert again == st


def test_redirect_start_needs_public_url(fake, http, cfg):
    c, t = connector(fake, http, replace(cfg, public_url=""))
    s = c.start()
    with pytest.raises(AuthError) as e:
        c.redirect_start(s["connect_id"])
    assert "OUTLOOK_PUBLIC_URL" in str(e.value)


def test_aadsts53003_is_policy_blocked(cfg):
    a = _auth_with(lambda r: httpx.Response(400, json={"error": "invalid_grant",
                                                        "error_description": "AADSTS53003: blocked"}), cfg)
    assert a.device_poll("dc-1") == ("policy_blocked", None)


# fix round B7 ----------------------------------------------------------------------------

def _me_fails(fake, monkeypatch, times):
    real, left = fake._graph, [times]

    def flaky(path, q):
        if path == "/me" and left[0]:
            left[0] -= 1
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        return real(path, q)
    monkeypatch.setattr(fake, "_graph", flaky)


def test_identity_lookup_failing_twice_fails_the_connect(fake, http, cfg, monkeypatch):
    c, t = connector(fake, http, cfg)
    s = c.start()
    fake.device_state = "connected"
    _me_fails(fake, monkeypatch, times=2)
    first = c.status(s["connect_id"])
    assert first["status"] == "failed" and "graph_error" in first["message"]
    assert not [tok for tok in fake.access_tokens | fake.refresh_tokens if tok in first["message"]]
    t[0] += 10
    assert c.status(s["connect_id"]) == first


def test_identity_lookup_is_retried_once(fake, http, cfg, monkeypatch):
    c, _ = connector(fake, http, cfg)
    s = c.start()
    fake.device_state = "connected"
    _me_fails(fake, monkeypatch, times=1)
    assert c.status(s["connect_id"])["status"] == "connected"


def test_redirect_identity_lookup_failure_fails_the_connect(fake, http, cfg, monkeypatch):
    c, _ = connector(fake, http, cfg)
    s = c.start()
    state = parse_qs(urlparse(c.redirect_start(s["connect_id"])).query)["state"][0]
    _me_fails(fake, monkeypatch, times=2)
    assert c.redirect_callback(state, "code-1").startswith("Sign-in failed")
    assert c.status(s["connect_id"])["status"] == "failed"
