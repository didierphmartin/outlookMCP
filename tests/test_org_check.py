import dataclasses
import os

import pytest

import org_check


@pytest.fixture
def org_cfg(cfg):
    return dataclasses.replace(cfg, org_tenant="tid-1", org_client_id="org-cid", org_client_secret="org-secret",
                               org_group="vip-group")


@pytest.fixture
def org(fake):
    fake.app_secret = "org-secret"
    fake.add_mailbox("u-alice", "alice@contoso.com", "Alice")
    fake.add_mailbox("u-eve", "eve@contoso.com", "Eve", in_group=False)
    return fake


def test_vip_readable_and_non_vip_refused(org, http, org_cfg, capsys):
    org.forbid_mailbox("u-eve")
    assert org_check.main(["u-alice", "u-eve"], http, org_cfg) == 0
    assert capsys.readouterr().out.splitlines() == ["token: ok", "group: 1 members", "vip: ok", "non-vip: refused"]


def test_non_vip_readable_fails(org, http, org_cfg, capsys):
    assert org_check.main(["u-alice", "u-eve"], http, org_cfg) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "non-vip: READABLE (scope not applied)"


def test_vip_refused_fails(org, http, org_cfg, capsys):
    org.forbid_mailbox("u-alice")
    org.forbid_mailbox("u-eve")
    assert org_check.main(["u-alice", "u-eve"], http, org_cfg) == 1
    assert "vip: forbidden" in capsys.readouterr().out


def test_bad_secret_stops_at_token_and_prints_no_secret(org, http, org_cfg, capsys):
    org.app_secret = "other"
    assert org_check.main(["u-alice", "u-eve"], http, org_cfg) == 1
    out = capsys.readouterr().out
    assert out.startswith("token: reconnect_needed") and "org-secret" not in out


def test_env_file_loaded_without_overriding(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('# c\nOUTLOOK_ORG_TENANT="t1"\nOUTLOOK_ORG_GROUP=g;x=y\nOUTLOOK_ORG_CLIENT_ID=from-file\n')
    monkeypatch.setenv("OUTLOOK_ORG_CLIENT_ID", "already")
    for k in ("OUTLOOK_ORG_TENANT", "OUTLOOK_ORG_GROUP"):
        monkeypatch.setenv(k, "x")      # registers restore; the next line clears it for the load
        monkeypatch.delenv(k)
    org_check.load_env(tmp_path / ".env")
    assert (os.environ["OUTLOOK_ORG_TENANT"], os.environ["OUTLOOK_ORG_GROUP"], os.environ["OUTLOOK_ORG_CLIENT_ID"]) == ("t1", "g;x=y", "already")
