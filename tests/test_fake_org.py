"""FakeGraph as an organization: app-only tokens, several mailboxes, one group."""
import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN = "https://login.microsoftonline.com/tid-1/oauth2/v2.0/token"


def app_token(http) -> dict:
    r = http.post(TOKEN, data={"grant_type": "client_credentials", "client_id": "cid", "client_secret": "s",
                               "scope": "https://graph.microsoft.com/.default"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_app_token_reads_a_member_mailbox(fake, http):
    fake.add_mailbox("u-alice", "alice@contoso.com", "Alice")
    r = http.get(f"{GRAPH}/users/u-alice/mailFolders", headers=app_token(http))
    assert r.status_code == 200
    assert {f["displayName"] for f in r.json()["value"]} >= {"Inbox"}


def test_app_token_refused_on_me(fake, http):
    assert http.get(f"{GRAPH}/me", headers=app_token(http)).status_code in (401, 403)


def test_forbidden_mailbox_is_403(fake, http):
    fake.add_mailbox("u-bob", "bob@contoso.com", "Bob")
    fake.forbid_mailbox("u-bob")
    assert http.get(f"{GRAPH}/users/u-bob/mailFolders", headers=app_token(http)).status_code == 403


def test_group_members_listed(fake, http):
    fake.add_mailbox("u-alice", "alice@contoso.com", "Alice")
    fake.add_mailbox("u-bob", "bob@contoso.com", "Bob")
    fake.add_mailbox("u-eve", "eve@contoso.com", "Eve", in_group=False)
    r = http.get(f"{GRAPH}/groups/{fake.group_id}/members/microsoft.graph.user", headers=app_token(http))
    assert r.status_code == 200
    assert r.json()["value"] == [{"id": "u-alice", "mail": "alice@contoso.com", "displayName": "Alice"},
                                 {"id": "u-bob", "mail": "bob@contoso.com", "displayName": "Bob"}]


def test_user_token_paths_unchanged(fake, http):
    at = fake._tokens_response().json()["access_token"]
    r = http.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {at}"})
    assert r.status_code == 200 and r.json()["mail"] == fake.account


def test_mailboxes_keep_their_own_messages_and_deltas(fake, http):
    fake.add_mailbox("u-alice", "alice@contoso.com", "Alice")
    fake.add_mailbox("u-bob", "bob@contoso.com", "Bob")
    fake.add_message(subject="mine")
    a = fake.add_message(subject="alice", mailbox="u-alice")
    h = app_token(http)
    r = http.get(f"{GRAPH}/users/u-alice/mailFolders/inbox/messages/delta", headers=h)
    assert [m["id"] for m in r.json()["value"]] == [a]
    assert "/users/u-alice/mailFolders/" in r.json()["@odata.deltaLink"]
    r = http.get(f"{GRAPH}/users/u-bob/mailFolders/inbox/messages/delta", headers=h)
    assert r.json()["value"] == []
    assert http.get(f"{GRAPH}/users/u-bob/messages/{a}", headers=h).status_code == 404
    assert http.get(f"{GRAPH}/users/u-alice/messages/{a}", headers=h).json()["toRecipients"][0][
        "emailAddress"]["address"] == "alice@contoso.com"


def test_subscription_on_a_mailbox_notifies_only_its_messages(fake, http):
    fake.add_mailbox("u-alice", "alice@contoso.com", "Alice")
    fake.add_mailbox("u-bob", "bob@contoso.com", "Bob")
    got = []
    fake.on_notification = got.append
    exp = "2026-10-26T10:00:00Z"
    fake.clock = lambda: 1793000000.0 - 3600
    r = http.post(f"{GRAPH}/subscriptions", headers=app_token(http), json={
        "changeType": "created", "resource": "users/u-alice/mailFolders('inbox')/messages",
        "expirationDateTime": exp, "clientState": "cs"})
    assert r.status_code == 201, r.text
    fake.add_message(subject="b", mailbox="u-bob")
    assert got == []
    mid = fake.add_message(subject="a", mailbox="u-alice")
    assert got[0]["value"][0]["resourceData"]["id"] == mid
