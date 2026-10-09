# Outlook MCP server

A small MCP server (FastAPI, streamable-HTTP JSON-RPC) that lets SynergyAI read a Microsoft 365 or
Outlook.com mailbox through Microsoft Graph. A user connects a mailbox once (device-code sign-in, or a
browser redirect where device code is blocked); the server keeps an encrypted refresh token in a local
SQLite file and hands the app a **connection key**, which the app sends as the `X-Outlook-Account`
header. The main tool, `poll_new_mail`, returns the next unseen email in a folder, oldest first, one per
call and never the same email twice, so a workflow's "Poll Outlook" code node can run on a cron. The
server only reads mail (`Mail.Read`): it never sends, moves or deletes anything.

## 1. Microsoft app registration

One Entra ID app registration serves every user and every tenant.

1. [Entra admin center](https://entra.microsoft.com) → **Entra ID → App registrations → New registration**.
2. Name: e.g. *SynergyAI Outlook*. **Supported account types:** *Accounts in any organizational
   directory and personal Microsoft accounts*. Leave the redirect URI empty for now. Register.
3. Copy the **Application (client) ID** into `OUTLOOK_CLIENT_ID`.
4. **Authentication → Settings** tab → **Allow public client flows: Yes.** Save.
5. **API permissions → Add a permission → Microsoft Graph → Delegated permissions:** `Mail.Read`,
   `User.Read`, `offline_access`.
6. Optional, for the browser sign-in fallback: **Authentication → Add a platform → Mobile and desktop
   applications**, redirect URI `<OUTLOOK_PUBLIC_URL>/auth/callback`
   (e.g. `https://synergy.example.com/outlook/auth/callback` or `http://localhost:8770/auth/callback`).
   The URL must be reachable **from the user's browser** (not just from gpt) and must be `https://`
   unless it is `localhost`: Microsoft refuses plain-http redirect URIs elsewhere.

**No client secret is needed.** The app is a public client: device code and the PKCE redirect both
work without one. `OUTLOOK_CLIENT_SECRET` is optional and only for a registration you have made
confidential (a "Web" platform redirect URI); leave it empty otherwise, because Microsoft rejects a
secret sent by a public client.

## 2. Corporate tenants

- The first user of a work tenant may see **"Need admin approval"**. A tenant admin grants consent
  once by opening `https://login.microsoftonline.com/<tenant>/adminconsent?client_id=<client id>`
  (`<tenant>` is the tenant's id or domain), or in the Entra admin center → **Enterprise
  applications** → the app → **Permissions → Grant admin consent for <tenant>**. After that, every
  user of the tenant can connect.
- Some tenants block device-code sign-in with **Conditional Access**. `connect_status` then answers
  `policy_blocked`. If `OUTLOOK_PUBLIC_URL` is set, it also returns a `redirect_url` that opens a
  normal browser sign-in (PKCE). Because a real tenant does not always report the block as such (the
  device sign-in may just expire, be denied or fail), `expired`, `denied` and `failed` carry the same
  `redirect_url` whenever `OUTLOOK_PUBLIC_URL` is set. Without `OUTLOOK_PUBLIC_URL` there is no
  fallback: set it, or ask IT to allow device code for this app.

## 3. Run

```bash
cp .env.example .env          # then fill in the values
./run.sh
```

| Variable | Required | Meaning |
|---|---|---|
| `OUTLOOK_CLIENT_ID` | yes | The app registration's client id. |
| `OUTLOOK_SECRET_KEY` | yes | Fernet key that encrypts refresh tokens at rest. Generate one with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Losing it means every mailbox must reconnect. |
| `OUTLOOK_PUBLIC_URL` | no | The server's address as the user's browser reaches it (`https://...`, or `http://localhost...`). Enables the redirect fallback. |
| `OUTLOOK_CLIENT_SECRET` | no | Only for a confidential registration (see above). |
| `OUTLOOK_PORT` / `OUTLOOK_HOST` | no | Default `8770` / `127.0.0.1`. |
| `OUTLOOK_DB_PATH` | no | Default `~/Documents/synergyAI/outlook_mcp.sqlite`. |
| `OUTLOOK_ATTACHMENT_MAX_BYTES` | no | `get_attachment` size cap, default 10 MB. |
| `OUTLOOK_PYTHON` | no | Interpreter `run.sh` uses to create `.venv` (3.10+), e.g. `OUTLOOK_PYTHON=/usr/local/bin/python3 ./run.sh`. |

**Never commit `.env`** (it is in `.gitignore`): it holds the encryption key and the client id.
Changing `OUTLOOK_SECRET_KEY` later makes every stored sign-in unreadable: the mailbox tools then
answer `reconnect_needed`.

**Keep this machine's clock NTP-synced.** On the first poll of a folder + filter set, "now" (the
point from which mail counts as new) is taken from this server's clock and compared with Graph's
`receivedDateTime`: a clock running ahead silently skips mail received in between.

## 4. Register in SynergyAI

**MCP Servers → Add**, name **Outlook**, URL `http://127.0.0.1:8770/mcp`. Test the connection: six
tools are listed. Then use **Connect to Outlook** in the workflow editor; it stores the connection key
as the server row's `X-Outlook-Account` header. Two mailboxes = two server rows.

- **Docker editions of gpt:** the backend runs in a container, so `127.0.0.1` is the container
  itself. Start this server with `OUTLOOK_HOST=0.0.0.0` and register the URL as
  `http://host.docker.internal:8770/mcp`.
- **Compiled scripts** (LangGraph and the other targets) read the header from the environment:
  `MCP_HEADERS_OUTLOOK={"X-Outlook-Account":"<connection key>"}`.

## 5. Tools

| Tool | Input | Output |
|---|---|---|
| `connect_start` | `{method?: "device"}` | `{connect_id, verification_uri, user_code, expires_in}` |
| `connect_status` | `{connect_id}` | `{status, account?, connection_key?, message?, redirect_url?}`; status is `pending`, `connected`, `expired`, `denied`, `policy_blocked`, `failed` (+ `message`) or `unknown` (no such `connect_id`, e.g. a typo or another server's database). With `OUTLOOK_PUBLIC_URL`, `policy_blocked`, `expired`, `denied` and `failed` add `redirect_url` (browser sign-in). The key is returned once. |
| `whoami` | — | `{account, tenant, connected_at}` |
| `list_folders` | — | `{folders: [{id, name, path}]}` |
| `poll_new_mail` | `{folder, from?, subject_contains?, has_attachments?}` | `{mail: {id, from, to, cc, subject, received_at, body_text, web_link, attachments} \| null}` |
| `get_attachment` | `{mail_id, attachment_id}` | `{name, content_type, size, text?, base64?}` |

Every tool publishes its result's JSON Schema (`outputSchema` in `tools/list`). A success carries the
result as `structuredContent`, and the same JSON as text for clients that read `content` only.

The mailbox tools need the `X-Outlook-Account` header. Every failure is an MCP tool error
(`isError: true`) whose text is `{"error": code, "message": ...}`, e.g. `not_connected`,
`reconnect_needed` (sign in again), `throttled`, `network_error`, `unknown_folder`, `too_large`.

`poll_new_mail` starts its cursor at **now** (this server's clock) on first use of a folder + filter
set (old mail is not replayed), and remembers what it delivered across restarts. If Graph drops the
delta state (410 Gone / SyncState errors), the server starts the folder's delta again from that same
starting point; already delivered mail is not delivered again. Message ids are immutable ids, so a
mail keeps its id when it is moved to another folder. `mail` is
`{id, from, to, cc, subject, received_at, body_text, web_link, attachments: [{id, name, content_type, size}]}`.

### Attachments stay unopened until after triage

An incoming email is untrusted until a triage step (typically a dispatcher) has judged it legitimate.
Attachments can carry prompt injection or files crafted against a parser, so:

- `poll_new_mail` lists attachments as **metadata only** and never downloads or parses them.
- People open attachments via the mail's `web_link` (Outlook, with its own protections).
- Agents open them with `get_attachment`, the **only** place content is downloaded or parsed. Give the
  Outlook server as a tool only to agents **downstream of triage**, on the branch for legitimate mail.
- Do not "optimise" attachment fetching back into `poll_new_mail`.

## 6. Fake mode

`OUTLOOK_FAKE_GRAPH=1 ./run.sh` runs against a built-in fake of Microsoft sign-in and Graph: no
Microsoft account, client id or secret key needed. Device sign-in succeeds on the first
`connect_status`. `OUTLOOK_FAKE_SEED=seed.json` preloads messages (a JSON list shaped like the
`/fake/messages` body). Extra endpoints, fake mode only:

| Endpoint | Body | Effect |
|---|---|---|
| `POST /fake/messages` | `{folder?, subject, sender, received?, attachments?: [{name, content_type, text}]}` | Adds a message; returns `{id}`. `received` defaults to now. |
| `GET /fake/requests` | — | Every request the server made to "Microsoft", e.g. to prove a poll fetched no attachment. |
| `POST /fake/device` | `{"state": "connected"}` | Next device-code answer: `pending`, `connected`, `blocked` (Conditional Access) or `declined`. |

`./smoke.sh` runs an end-to-end check in fake mode on port 8779.

## Tests

```bash
.venv/bin/python -m pytest -q
```
