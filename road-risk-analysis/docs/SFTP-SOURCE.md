# SFTP video source

An SFTP server (e.g. CAMTRACK `bi.camtrack.mg`) is one more provider of the cloud-import
pipeline, next to Google Drive and OneDrive:

    browse the server → select files / folders → import batch → download (progress, resume)
    → verify size → processing job with the chosen models

Everything you know from the Drive imports applies (*Cloud sources* page, *Imports* tab,
already-imported badges, cancel / retry, watched folder + auto-import). What is specific to
SFTP is below.

## 1. The password: a secrets file, never the UI

The connection stores the host, port, username, base path and a **password key** (e.g.
`CAMTRACK`). The password itself is only in a secrets file that the API and the worker read at
each connection. It is never stored in the database, shown in the web console, returned by the
API, written to a log, sent to git or baked into a Docker image.

File format (`infra/secrets/sftp.env`, template `infra/secrets/sftp.env.example`):

```env
# SFTP_PASSWORD_<KEY>=<password>      — <KEY> = the password key of the server
SFTP_PASSWORD_CAMTRACK=the-password

# optional: key-based auth instead (file in the same directory, plain name)
# SFTP_KEYFILE_CAMTRACK=camtrack_ed25519
# SFTP_KEY_PASSPHRASE_CAMTRACK=
```

No quotes needed (matching quotes are stripped); `#`, `=` and spaces inside the value are kept.
The file is re-read at each connection — no restart after editing it.

### On the server (v2 stack)

```bash
cd ~/viasafe-v2/infra/secrets
cp sftp.env.example sftp.env
chmod 600 sftp.env
nano sftp.env            # SFTP_PASSWORD_CAMTRACK=…
```

Exact path: **`~/viasafe-v2/infra/secrets/sftp.env`**, mode **600**. It is mounted read-only at
`/app/secrets` in the `api` and `worker` containers (`infra/deploy/server-v2/docker-compose.v2.yml`:
`../../secrets:/app/secrets:ro`). `push.sh` never copies `secrets/*.env` (nor key files), so the
file on the server is yours: create it once, it survives every redeploy. After the first deploy
with this feature, run `./deploy.sh` so the containers get the new mount and the `paramiko`
image layer.

### On the dev PC

`road-risk-analysis/infra/secrets/sftp.env` (`chmod 600`). `infra/docker-compose.yml` and
`infra/docker-compose.demo.yml` mount `./secrets:/app/secrets:ro` (= `infra/secrets`).
Outside a container (tests, scripts) the code falls back to `infra/secrets` of the checkout
(or `VIASAFE_SECRETS_DIR`).

**Local test stack (`roaddemo_api_1`)** — the container mounts `infra/secrets` (edits are seen
at the next connection) but runs a stale image without paramiko. After each re-creation:

```bash
docker exec roaddemo_api_1 pip install paramiko==4.0.0
docker restart roaddemo_api_1 && infra/start-local-test.sh     # restart API + worker on new code
```

Alternative: an environment variable of the same name (`SFTP_PASSWORD_CAMTRACK`) is used when
the file has no entry for the key — handy for a throwaway test, not for production.

## 2. Add, edit and remove servers

*Cloud sources → Add a source → SFTP server*:

| Field | CAMTRACK |
|-------|----------|
| Host | `bi.camtrack.mg` |
| Port | `22` |
| Username | `roadusertoread` |
| Base path | `/home/Ymane-New/ft_violations` (empty = the user's home) |
| Password key | `CAMTRACK` → the API reads `SFTP_PASSWORD_CAMTRACK` |

The card then shows the live state (**Disconnected / Connecting… / Connected / Error**), the base
folder, whether the password was found in the secrets file (never its value) and the trusted
host-key fingerprint, with:

- **Connect / Test** — opens a session and lists the base folder; **Reconnect** once connected.
- **Disconnect** — closes the browse session (downloads in progress keep their own session).
- **Edit server…** — host, port, username, base path, password key, name. A new host or port
  forgets the trusted host key.
- **Forget host key** — see §5.
- **Remove** (menu, with confirmation) — removes the profile, its host key and its log. Jobs
  already imported stay. The password line stays in `sftp.env`: delete it there if unused.

Add as many servers as needed (one password key each). API: `POST /cloud-connections/sftp`,
`PATCH /cloud-connections/{id}/sftp`, `DELETE /cloud-connections/{id}`, see `docs/openapi.json`
(tag *Cloud connections*). Permissions: same as the other connections (module *jobs*: view to
browse / connect / disconnect, add to create servers and imports, edit to change a server, forget
its host key or clear its log, delete to remove it); every change is audited
(`cloud_connection.create / update / delete / sftp_connect / host_key_forget`).

## 3. Browsing and importing

- Browsing starts at the base path (or the home folder) and **cannot leave it**: every requested
  path is normalised and refused when it is outside (`..`, other absolute paths). (A symlink
  *inside* the base folder that the server resolves elsewhere is still followed — the
  account's own server-side permissions are the real boundary; use a read-only account.)
  Folders, parent (←, Backspace), breadcrumbs, sizes and dates, search by name in the current folder.
- Only videos can be imported: `.mp4 .mov .avi .mkv .h264 .ts` (any case). Other files are listed
  greyed out.
- Select files and/or whole folders (+ *include sub-folders*), choose the models → **Import**.
- **Already imported**: a file is the same when host + port + path + size + modification time
  are the same; re-importing it is skipped (*skipped_duplicate*) unless *Import again* is ticked. A
  file whose size or date changed shows *Changed* and is imported again.
- Each file is downloaded by the worker into the shared media directory (`.part` file, live
  progress in the *Imports* tab), its size checked against the server's, then handed to the
  processing queue as a job (`source_type` = `sftp`, `source_ref` = remote path).
- At most `processing.cloud_import_concurrency` downloads run at once, and **at most 2 per
  server** (polite with servers that drop busy sessions).

## 4. The connection log

Under the file browser, *Connection log* (collapsible, copy, clear) shows the last 500 lines of
the server, FileZilla-style, written by the API (connect, listings) and by the worker (downloads,
tagged *download*). It refreshes every 1.5 s while the page is visible.

| Line | Meaning |
|------|---------|
| `Status: Connecting to bi.camtrack.mg...` | TCP connection (timeout 15 s) |
| `Status: Host key trusted on first use: ssh-ed25519 SHA256:…` | first connection: the key is now stored |
| `Status: Using username "roadusertoread".` | SSH handshake done, authenticating (timeout 20 s) |
| `Status: Connected to bi.camtrack.mg` | authenticated, SFTP session open |
| `Status: Retrieving directory listing of "/path"...` / `Listing directory /path` / `Directory listing of "/path" successful` | browsing |
| `Status: Starting download of /path/file.mp4` | worker download |
| `Status: Resuming download of /path/file.mp4 at 6.0 MB of 48.3 MB` | retry after a failure, continues the `.part` file |
| `Status: File transfer successful, transferred 457.5 KB in 7 seconds` | bytes of this attempt |
| `Status: Retrying in 4 seconds (attempt 2/8), will resume at 6.0 MB...` | automatic retry (backoff 2, 4, 8, 16, 30 … s) |
| `Status: Disconnected from server` | session closed (end of download, Disconnect, 3 min idle) |
| `Error: Could not connect to server` | TCP / DNS / handshake failed (see the line above it) |
| `Error: Connection timed out after 15 seconds of inactivity` | server unreachable or silent |
| `Error: FATAL ERROR: Remote side unexpectedly closed network connection` | the server dropped the session (retried) |
| `Error: Authentication failed. …` | wrong username / password (not retried) |
| `Error: Host key mismatch for …` | the server key changed (not retried, §5) |
| `Error: No password set for this server: add SFTP_PASSWORD_X=…` | missing line in `sftp.env` |

API: `GET /cloud-connections/{id}/sftp/log?after=<last_seq>` (lines + link state),
`DELETE …/sftp/log`. Lines live in Redis (`cloud:sftp:log:<id>`), never contain a password.

## 5. Troubleshooting

**Authentication failed** — check the username on the card and the line
`SFTP_PASSWORD_<KEY>=…` in the secrets file of *that* machine (server:
`~/viasafe-v2/infra/secrets/sftp.env`). The card says *Password: Missing* when the key has no line.
On the local stack, copy the file into the container again after editing it (§1). Then
**Connect / Test**.

**Host key changed** (`Host key mismatch … Refusing to connect`) — the server presents a
different key than the one trusted at the first connection. If the change is expected
(reinstalled / migrated server; confirm the new fingerprint with the server admin:
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`), use **Forget host key** then **Connect**:
the new key is trusted and stored. Otherwise do not connect — it may be a man-in-the-middle.

**The server drops the connection** (`FATAL ERROR: Remote side unexpectedly closed network
connection`, typical of CAMTRACK) — nothing to do in most cases: browsing reconnects and retries
(3 times), downloads retry with backoff up to 8 times in a row and **resume where they stopped**
(the counter resets whenever the previous attempt moved forward, so a long file still finishes).
If an import ends *failed*, **Retry** it in the *Imports* tab: it resumes the partial file. If it
keeps failing, lower `processing.cloud_import_concurrency`, or ask the server admin about
`MaxStartups` / `MaxSessions` / idle timeouts.

**Could not connect / timed out** — check from the server running VIASafe:
`nc -vz bi.camtrack.mg 22` (or the banner probe below); firewalls often allow only some source
IPs.

```bash
python3 - <<'EOF'
import socket; s = socket.create_connection(("bi.camtrack.mg", 22), timeout=15); print(s.recv(256))
EOF
```

**`No module named 'paramiko'`** — the image predates this feature: rebuild (`./deploy.sh` on the
server) or, on the local test container, `docker exec roaddemo_api_1 pip install paramiko==4.0.0`.

## 6. Implementation notes

- `app/modules/m07_ingestion/sftp_core.py` — pure rules (secrets parsing, path confinement, video filter,
  dedupe key, resume offset, host-key decision, log lines), unit-tested in `tests/test_sftp_source.py`.
- `app/modules/m07_ingestion/sftp_source.py` — paramiko sessions (connect per operation; the API keeps one
  browse session per server for 3 min with SSH keepalive), the `SftpProvider` registered in
  `cloud_providers`, the Redis log / link state.
- The import worker (`cloud_import.run_item`, `import_tasks`) is the same as for drives; SFTP adds
  the per-server download slots and the resume-aware retry count.
- Timeouts: TCP connect 15 s, banner / key exchange / auth 20 s, SFTP reads 30 s, keepalive 15 s.
