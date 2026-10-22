# Outlook MCP server

A small MCP server (FastAPI, streamable-HTTP JSON-RPC) that lets SynergyAI read a Microsoft 365 or
Outlook.com mailbox through Microsoft Graph. A user connects a mailbox once (device-code sign-in, or a
browser redirect where device code is blocked); the server keeps an encrypted refresh token in a local
SQLite file and hands the app a **connection key**, which the app sends as the `X-Outlook-Account`
header. The main tool, `poll_new_mail`, returns the next unseen email in a folder, oldest first, one per
call and never the same email twice, so a workflow's "Poll Outlook" code node can run on a cron. After
triage, `move_mail` files an email into a folder and `forward_mail` sends it on to other addresses
(`Mail.ReadWrite`, `Mail.Send`). The server never deletes anything.

## 1. Microsoft app registration

One Entra ID app registration serves every user and every tenant.

1. [Entra admin center](https://entra.microsoft.com) → **Entra ID → App registrations → New registration**.
2. Name: e.g. *SynergyAI Outlook*. **Supported account types:** *Accounts in any organizational
   directory and personal Microsoft accounts*. Leave the redirect URI empty for now. Register.
3. Copy the **Application (client) ID** into `OUTLOOK_CLIENT_ID`.
4. **Authentication → Settings** tab → **Allow public client flows: Yes.** Save.
5. **API permissions → Add a permission → Microsoft Graph → Delegated permissions:** `Mail.ReadWrite`
   (read and move), `Mail.Send` (forward), `User.Read`, `offline_access`. A registration made for an
   earlier version (with `Mail.Read` only) needs the two added and, on a work tenant, admin consent
   granted again; every mailbox connected before that must **connect again**, because its sign-in
   carries the old permissions (`move_mail` and `forward_mail` then answer `forbidden`).
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

**Keep this machine's clock NTP-synced.** The point from which mail counts as new (the sign-in, or
"now" for a later folder + filter set) is taken from this server's clock and compared with Graph's
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
| `connect_start` | `{method?: "device"}` | `{connect_id, verification_uri, user_code, expires_in, redirect_url?}`; with `OUTLOOK_PUBLIC_URL`, `redirect_url` is a browser sign-in for the same `connect_id`, offered from the start because a tenant can refuse the code on Microsoft's page without the device poll ever saying so. |
| `connect_status` | `{connect_id}` | `{status, account?, connection_key?, credential_header?, message?, redirect_url?}`; status is `pending`, `connected`, `expired`, `denied`, `policy_blocked`, `failed` (+ `message`) or `unknown` (no such `connect_id`, e.g. a typo or another server's database). With `OUTLOOK_PUBLIC_URL`, `policy_blocked`, `expired`, `denied` and `failed` add `redirect_url` (browser sign-in). The key is returned once, with `credential_header` (`X-Outlook-Account`): the header the app must send it in. |
| `whoami` | — | `{account, tenant, connected_at}`, once Microsoft has confirmed the sign-in still works (a refused sign-in is `reconnect_needed`) |
| `list_folders` | — | `{folders: [{id, name, path}]}` |
| `poll_new_mail` | `{folder, mode?: "new" \| "drop", from?, subject_contains?, has_attachments?}` | `{mail: {id, attempt, from, to, cc, subject, received_at, body_text, web_link, attachments} \| null}` |
| `get_attachment` | `{mail_id, attachment_id, raw?}` | `{name, content_type, size, text?, base64?}`; with `raw: true` always `{name, content_type, size, base64}`, never `text` |
| `move_mail` | `{mail_id, folder}` | `{id, folder, created}`; files the email into `folder` (a name or path such as `Dispatched/Sales`), creating missing folders along the path (`created` lists them). The id does not change. Do not move mail into the folder `poll_new_mail` watches: it would come back as new. |
| `forward_mail` | `{mail_id, to, comment?}` | `{id, forwarded_to}`; forwards the email with its attachments from the connected mailbox. `to` is a list or one string separated by `,` or `;`. Graph has no per-message *redirect* (keeping the original sender): this is a forward. |
| `wait_for_new` | `{poll?: "poll_new_mail", args?: {folder, from?, ...}, timeout_s?: 120}` | `{status: ready \| timeout, via?: event \| pending \| fallback, events: on \| off, reason?}`; waits until the poll with these `args` probably has new mail. Call that poll next, until it returns null; on `timeout`, wait again. See §6. |

Every tool publishes its result's JSON Schema (`outputSchema` in `tools/list`). A success carries the
result as `structuredContent`, and the same JSON as text for clients that read `content` only.

The mailbox tools need the `X-Outlook-Account` header. Every failure is an MCP tool error
(`isError: true`) whose text is `{"error": code, "message": ...}`, e.g. `not_connected`,
`reconnect_needed` (sign in again), `forbidden` (the sign-in lacks `Mail.ReadWrite` or `Mail.Send`:
grant it, then connect again), `invalid_argument`, `throttled`, `network_error`, `unknown_folder`,
`too_large`.

`poll_new_mail` starts a connection's first folder + filter set at the **sign-in** (mail sent after
Connect counts, even before the first poll; older mail is not replayed), and any later folder + filter
set at **now** (a changed filter replays nothing), and remembers what it delivered across restarts. If Graph drops the
delta state (410 Gone / SyncState errors), the server starts the folder's delta again from that same
starting point; already delivered mail is not delivered again. Message ids are immutable ids, so a
mail keeps its id when it is moved to another folder. `mail` is
`{id, attempt, from, to, cc, subject, received_at, body_text, web_link, attachments: [{id, name, content_type, size, is_inline, kind}]}`.

- **`mode`** is `"new"` (default: mail received after the watch started, delivered once) or `"drop"`: any
  email that arrives in the folder counts, whatever its received date (a drop folder is created when
  missing). A drop-mode email still in the folder `DROP_LEASE_S` seconds (default 600) after it was delivered
  is delivered again, up to 3 deliveries in all, so move it out of the folder once handled. `wait_for_new`
  takes the same `mode` inside its `args`.
- **`attempt`** is which delivery of the email this is: always 1 in `new` mode; 2 or 3 in `drop` mode when the
  email stayed in the folder past the lease.
- **`is_inline`** is true for an attachment embedded in the body (a logo, a picture) rather than a file the
  sender attached.
- **`kind`** is `"file"` (a stored file), `"item"` (an attached email) or `"reference"` (a cloud link to a
  OneDrive/SharePoint file: Graph has no content for it, so `get_attachment` answers `cloud_attachment` and
  `mail_to_pdf` lists it as "(cloud link, not stored)").

### Attachments stay unopened until after triage

An incoming email is untrusted until a triage step (typically a dispatcher) has judged it legitimate.
Attachments can carry prompt injection or files crafted against a parser, so:

- `poll_new_mail` lists attachments as **metadata only** and never downloads or parses them.
- People open attachments via the mail's `web_link` (Outlook, with its own protections).
- Agents open them with `get_attachment`, the **only** place content is downloaded or parsed. Give the
  Outlook server as a tool only to agents **downstream of triage**, on the branch for legitimate mail.
- Do not "optimise" attachment fetching back into `poll_new_mail`.

`get_attachment` converts to text where it can (PDF, Word, plain text) and otherwise returns `base64`. With
**`raw: true`** it always returns the original bytes as `base64` and never `text`, e.g. to file a PDF as it
came. The same size cap applies (`OUTLOOK_ATTACHMENT_MAX_BYTES`; over it is a `too_large` error). An attached
email (a Graph item attachment) comes back as its MIME, named `<subject>.eml`, with content type
`message/rfc822`. A cloud-link attachment (`kind: "reference"`) has no content: `get_attachment` answers the
error `cloud_attachment`.

## 6. Events (wait instead of polling on a timer)

`wait_for_new` lets a workflow sleep until mail arrives instead of polling every few minutes. With
**events on**, Microsoft Graph sends a change notification for each new email in the watched folder to an
**Azure Event Hub**; this server reads the hub over an **outbound** connection (AMQP 5671, or WebSockets
443): no public URL, no inbound port. A notification only wakes the waiters: the next `poll_new_mail`
(delta + queue) still decides what is new, so a lost or repeated notification is harmless.

`wait_for_new` answers `ready` with `via`:

- `event`: a notification arrived for the folder (live: ~1 minute after the mail).
- `pending`: undelivered mail is already queued, or a notification came while nobody was waiting.
- `fallback`: the safety poll is due (`WATCH_FALLBACK_S`, default 900 s), and on the first wait for a
  folder after the server starts (mail may have come while it was down). With **events off** this is the
  only way to become ready: plain timed polling, and `reason` says why events are off.

It answers `timeout` after `timeout_s` (at most 150, under the 180 s tool timeout of SynergyAI's compiled
scripts). Waits are asynchronous: many waiting workflows hold no worker threads.

**Subscriptions** are created by the first wait on a folder (one per connection and folder, shared by every
filter set), renewed every few days while something waits on them (Graph caps mail subscriptions at 10,070
minutes), and deleted after a day without waits. Graph's lifecycle notifications are handled: a removed
subscription is recreated and its folder woken to catch up. They need no extra permission (`Mail.Read`
is enough). A refused create is retried every 15 minutes, meanwhile the folder polls on the fallback.

**Set-up** (once per install, Azure portal):

1. *Event Hubs* → **+ Create**: a namespace (Basic tier is enough), then on it **+ Event Hub**, e.g. `outlook-mail`.
2. Open the **hub** (Entities → Event Hubs → the hub) → **Access control (IAM)** → **+ Add** → **Add role
   assignment** → **Azure Event Hubs Data Sender** → **Next** → **+ Select members** → *Microsoft Graph
   Change Tracking* → **Select** → **Review + assign**. If it is not found, add it once in Cloud Shell:
   `az ad sp create --id 0bf30f3b-4a52-48df-9a82-234910c4a086`.
3. On the hub: **Settings → Shared access policies → + Add**, `listen`, **Listen** only; copy its
   *Connection string–primary key* (it ends with `;EntityPath=<hub>`).
4. `.env`: `EVENTHUB_CONNECTION=<that string>` and
   `EVENTHUB_NOTIFICATION_URL=EventHub:https://<namespace>.servicebus.windows.net/eventhubname/<hub>?tenantId=<tenant domain>`;
   restart the server.

Read positions are saved in the server's database, so a restart resumes where it stopped (the hub keeps
events at least a day). Run **one** server per hub and consumer group (`EVENTHUB_CONSUMER_GROUP`):
two would split the hub's partitions and each miss notifications.

Graph also drops a "Validation: Testing client application reachability" message into the hub when a
subscription is created; it names no subscription and is ignored, as is any notification whose
`clientState` is not the subscription's secret.

## 7. Fake mode

`OUTLOOK_FAKE_GRAPH=1 ./run.sh` runs against a built-in fake of Microsoft sign-in and Graph: no
Microsoft account, client id or secret key needed. Device sign-in succeeds on the first
`connect_status`. `OUTLOOK_FAKE_SEED=seed.json` preloads messages (a JSON list shaped like the
`/fake/messages` body). Extra endpoints, fake mode only:

| Endpoint | Body | Effect |
|---|---|---|
| `POST /fake/messages` | `{folder?, subject, sender, received?, attachments?: [{name, content_type, text}]}` | Adds a message; returns `{id}`. `received` defaults to now. |
| `GET /fake/requests` | — | Every request the server made to "Microsoft", e.g. to prove a poll fetched no attachment. |
| `POST /fake/device` | `{"state": "connected"}` | Next device-code answer: `pending`, `connected`, `blocked` (Conditional Access) or `declined`. |

In fake mode events are on without a hub: a fake message added to a watched folder notifies in-process, so
`wait_for_new` answers `ready` / `event` at once.

`./smoke.sh` runs an end-to-end check in fake mode on port 8779.

## Tests

```bash
.venv/bin/python -m pytest -q
```
