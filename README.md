# McKesson Deployment Tool

Local deployment automation that runs **only inside the McKesson VDI laptop**.

It replaces the manual WinSCP + PuTTY deployment of the Aiden Java services onto
the McKesson app server with a single browser dashboard: select a service, paste
the checksum, verify, deploy, watch the live logs.

> **This application is completely independent from the Aiden laptop and the
> Aiden Deployment Tool.** There is no network call, no shared file, no API and
> no synchronisation between them. The checksum is copied by hand from the Aiden
> application and pasted into this one. That is the only link between the two.

---

## 1. Architecture

```
Browser (HTML + CSS + vanilla JS)
    |  fetch()            -> REST     /api/...
    |  WebSocket          <- events   /ws/logs
    v
FastAPI (Uvicorn, localhost only)
    |
    +-- config.py              service registry + every input validator
    +-- download_service.py    JAR download from the installation hub
    +-- ssh_service.py         Paramiko transport, the ONLY command executor
    +-- sftp_service.py        single-file upload, sudo tee for root-owned files
    +-- backup_service.py      dated JAR + unit-file backups
    +-- checksum_service.py    safe APP_CHECKSUM rewrite (no vim)
    +-- deployment_service.py  stage orchestration + port-conflict handling
    +-- log_service.py         journalctl -f streaming + event broadcaster
    |
    v  SSH / SFTP
McKesson app server  vm-mms-cims02.na.corp.mckesson.com (10.15.128.5)
```

The frontend is plain HTML5, CSS3 and vanilla JavaScript served directly by
FastAPI. There is no React, no build step, no bundler and no TypeScript.

### Stage pipeline

```
VALIDATE -> DOWNLOAD -> CONNECT -> BACKUP -> UPLOAD -> UPDATE CHECKSUM
   -> DAEMON RELOAD -> RESTART -> HEALTH CHECK -> LIVE LOGS -> SUCCESS
```

Each stage reports `○ waiting`, `⟳ running`, `✓ completed` or `✕ failed`.
**Any failure stops the deployment** — remaining stages are marked skipped and
the exact stage and error are shown. Nothing continues blindly.

### How it maps to the old manual process

| Manual step | Automated by |
|---|---|
| Download JAR from installation API | `download_service.py` |
| WinSCP connect, `CopyData/<date>/`, upload | `sftp_service.py` |
| PuTTY login, `sudo -i` | `ssh_service.py` |
| `mkdir <date>`, back up JAR | `backup_service.py` |
| Back up unit file in `/etc/systemd/system` | `backup_service.py` |
| Edit `APP_CHECKSUM` in vim | `checksum_service.py` (programmatic, verified) |
| `systemctl daemon-reload` | `deployment_service.daemon_reload()` |
| Copy JAR into `binaries/` | `sftp_service.upload_jar()` |
| `systemctl restart <service>` | `deployment_service.restart_service()` |
| Check status / `tail -200f` | health check + `/ws/logs` live stream |
| `lsof -i:<port>` / `kill <PID>` | port-conflict dialog, **confirmation required** |

---

## 2. Requirements

- Windows (McKesson VDI laptop)
- Python 3.10 or newer (developed on 3.12)
- Network access from the VDI to:
  - `vm-mms-cims02.na.corp.mckesson.com` (or `10.15.128.5`) on port 22
  - the installation hub URL
- An account on the app server (`day6sio`) that can `sudo`

---

## 3. Python setup

```powershell
cd mckesson-deployment-tool
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

> The zip does not contain `venv/`. Always create a fresh one on the machine
> where the tool will run.

---

## 4. Environment variables

Copy the template and edit it. **`.env` must never be committed or shared.**

```powershell
Copy-Item .env.example .env
notepad .env
```

| Variable | Purpose |
|---|---|
| `APP_HOST` / `APP_PORT` | Where the dashboard listens. Default `127.0.0.1:5002`. |
| `DRY_RUN` | `true` = simulate everything. Start here. |
| `DRY_RUN_CONNECT` | `false` = fully offline. `true` = real SSH, read-only checks only. |
| `SSH_HOST` / `SSH_ADDRESS` | Hostname, with the IP as a DNS fallback. |
| `SSH_USERNAME` | `day6sio` |
| `SSH_KEY_PATH` | Private key path. Preferred over a password. |
| `SSH_PASSWORD` | Fallback only. Never hardcoded anywhere else. |
| `SSH_HOST_KEY_POLICY` | `strict` (recommended) or `auto_add` for first connect. |
| `USE_SUDO` / `SUDO_PASSWORD` | Replaces `sudo -i`. Leave the password empty if NOPASSWD. |
| `INSTALLATION_HUB_URL` | Installation API base path. |
| `INSTALLATION_CODE` | Installation code. Backend only — never sent to the browser. |
| `REMOTE_BINARIES_DIR` | Default `/home/AidenAI/binaries` |
| `BACKUP_LAYOUT` | `nested` → `backups/2026-08-14/`, `flat` → `Aug14/` |
| `CHECKSUM_PATTERN` | Accepted checksum format. Default: 64-char SHA-256 hex. |
| `KEEP_TEMP_FILES` | `false` deletes the downloaded JAR after success. |

No password, key or installation code is ever exposed to the frontend.
`GET /api/config` returns only non-sensitive display values.

---

## 5. SSH setup

Key authentication is strongly preferred:

```powershell
ssh-keygen -t rsa -b 4096
type $env:USERPROFILE\.ssh\id_rsa.pub
# append that line to /home/day6sio/.ssh/authorized_keys on the app server
```

Then set `SSH_KEY_PATH=C:/Users/<you>/.ssh/id_rsa` in `.env`.

**Host keys.** With `SSH_HOST_KEY_POLICY=strict` the host must already be in
`known_hosts`. Connect once with PuTTY or `ssh day6sio@vm-mms-cims02...` and
accept the fingerprint, or temporarily set `auto_add` for the first connection.

**sudo.** The tool never opens an interactive `sudo -i` shell; it prefixes the
specific commands it needs. If the account has NOPASSWD sudo, leave
`SUDO_PASSWORD` empty. Otherwise set it, and the password is piped to `sudo -S`
— it is never written to a file or the log.

---

## 6. Starting the application

```powershell
.\venv\Scripts\python.exe run.py
```

Then open <http://localhost:5002>.

The host and port come from `APP_HOST` / `APP_PORT` in `.env` (default
`127.0.0.1:5002`). To use a different port, change `APP_PORT` — you do not need
to edit any command. The equivalent explicit form is:

```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 5002
```

Keep `APP_HOST=127.0.0.1`. Do not expose this port on the network — the process
holds credentials for the app server.

---

## 7. Configuring services

All services live in one dictionary in `app/config.py`. To add a fourth:

```python
SERVICES = {
    ...
    "my-new-service": {
        "display_name": "My New Service",
        "jar_prefix": "my-new-service",        # artifactId; JAR = <prefix>-<version>.jar
        "systemd_service": "aiMyNewService.service",
        "default_version": "1.6.0",
        "default_port": 8099,                  # used by the port-conflict check
    },
}
```

Restart the app. The dropdown, validation allowlist, JAR mapping and unit-file
allowlist all follow automatically. A service that is not in this dictionary
can never be deployed, restarted or backed up.

Currently configured:

| Key | Service | JAR | Unit | Port |
|---|---|---|---|---|
| `tx-test-mgmt` | TX Test Management | `tx-test-mgmt-1.6.0.jar` | `aiTXTestMgmt.service` | 8096 |
| `ai-dap-app` | AI DAP App | `ai-dap-app-1.6.0.jar` | `aiDAPApp.service` | 80 |
| `tx-integration-agent` | TX Integration Agent | `tx-integration-agent-1.6.0.jar` | `aiTXIntegrationAgent.service` | 9091 |

The **Version** field on the dashboard overrides `default_version` for a
one-off deployment (e.g. `1.7.0` → `tx-test-mgmt-1.7.0.jar`).

---

## 8. Dry-run mode

With `DRY_RUN=true` the tool will **never**:

- modify a file on the server
- upload anything
- restart a service
- kill a process

Instead every stage reports what it *would* do:

```
DRY RUN - no server file, service or process will be modified
Would download: tx-test-mgmt-1.6.0.jar from the installation hub
Would create backup directory: /home/AidenAI/binaries/backups/2026-08-14
Would back up jar:  /home/AidenAI/binaries/tx-test-mgmt-1.6.0.jar -> .../backups/2026-08-14/...
Would back up unit: /etc/systemd/system/aiTXTestMgmt.service -> .../backups/2026-08-14/...
Would upload tx-test-mgmt-1.6.0.jar to /home/AidenAI/binaries/tx-test-mgmt-1.6.0.jar
Would update APP_CHECKSUM in /etc/systemd/system/aiTXTestMgmt.service
Would execute: systemctl daemon-reload
Would execute: systemctl restart aiTXTestMgmt.service
DRY RUN COMPLETE
```

Recommended rollout on the McKesson laptop:

1. `DRY_RUN=true`, `DRY_RUN_CONNECT=false` — verify the UI and the pipeline offline.
2. `DRY_RUN=true`, `DRY_RUN_CONNECT=true` — verify SSH, sudo and that the unit
   file and existing JAR are found. Read-only commands run for real; nothing is
   modified.
3. `DRY_RUN=false` — real deployment. The UI shows a red **LIVE DEPLOYMENT**
   badge and asks for confirmation before starting.

---

## 9. Performing a deployment

1. On the **Aiden laptop**, copy the checksum from the Aiden application.
2. On the **McKesson laptop**, open <http://localhost:5002>.
3. Select the service. Set a version only if it differs from the default.
4. Paste the checksum and click **Verify checksum**. Deploy stays disabled
   until this succeeds.
5. Click **Deploy** and confirm.
6. Watch the pipeline and the live log pane. On success the service is running
   and `journalctl -u <unit> -f` continues streaming into the browser.

**If a port is occupied** the deployment stops and a dialog lists every process
holding the port (PID, command, user, socket). You can **View process**,
**Kill process** or **Cancel**. Nothing is killed without an explicit
confirmation, and after a successful kill the tool restarts the service, checks
its status and resumes the live logs.

**Rollback** is a manual copy from the backup folder shown in the log, e.g.:

```bash
sudo cp /home/AidenAI/binaries/backups/2026-08-14/tx-test-mgmt-1.6.0.jar /home/AidenAI/binaries/
sudo cp /home/AidenAI/binaries/backups/2026-08-14/aiTXTestMgmt.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart aiTXTestMgmt.service
```

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Host key ... is not in known_hosts` | Connect once with PuTTY and accept the fingerprint, or set `SSH_HOST_KEY_POLICY=auto_add`. |
| `Authentication failed for day6sio@...` | Wrong `SSH_KEY_PATH`, or the public key is not in `authorized_keys`. |
| `Could not reach ...` | VDI cannot see the server. Try `SSH_ADDRESS=10.15.128.5`; check VPN. |
| `sudo requires a password` | Set `SUDO_PASSWORD` in `.env`, or grant NOPASSWD sudo. |
| `Installation hub rejected the request (HTTP 401/403)` | Wrong `INSTALLATION_CODE`. |
| `... was not found on the installation hub (HTTP 404)` | Version does not exist. Check the Version field. |
| `Downloaded ... is not a JAR archive` | The hub returned an HTML error page. Check the URL and code. |
| `No Environment="APP_CHECKSUM=..." line found` | The unit file does not match the expected format. Fix it manually; the tool refuses to guess. |
| `Found 2 APP_CHECKSUM lines` | Duplicate entries in the unit file. Clean it up manually. |
| `A backup already exists at ...` | You already deployed today. Tick **Overwrite an existing backup** to proceed. |
| `Permission denied writing ...` | `day6sio` cannot write to the staging directory. Check `REMOTE_COPYDATA_DIR`. |
| Service failed to start | Read the journal output in the log pane; roll back from the backup folder. |
| Live logs never appear | The account cannot read the journal. Add it to `systemd-journal`, or keep `USE_SUDO=true`. |
| WebSocket shows `reconnecting…` | The backend stopped. Check the terminal running uvicorn. |

A timestamped record of every deployment is appended to `temp/deployments.log`.

---

## 11. Security

- **No arbitrary command execution.** There is no `/execute-command` endpoint.
  Every route maps to one specific operation: `download_jar`, `backup_jar`,
  `backup_service`, `upload_jar`, `update_checksum`, `daemon_reload`,
  `restart_service`, `get_service_status`, `stream_logs`, `find_port_process`,
  `kill_process`.
- **Allowlists everywhere.** Service keys, systemd unit names, JAR filenames,
  versions, ports and PIDs are validated in `app/config.py` before reaching the
  server. A unit name is rejected unless it belongs to a registered service, so
  `sshd.service` can never be restarted or overwritten.
- **No path traversal.** Remote paths are built only from config constants plus
  validated basenames; `..`, `/` and backslashes are rejected.
- **No shell injection.** Commands are argv lists joined with `shlex.join`; user
  input is never interpolated into a shell string. Checksums containing quotes,
  whitespace, `$` or backticks are rejected before they reach the unit file.
- **PID 1 is never killable**, and killing anything requires `confirmed=true`
  plus a browser confirmation.
- **Only the selected service is touched** — one unit file backed up, one unit
  file edited, one service restarted, one JAR uploaded.
- **Backup before modification**, and an existing backup is never overwritten
  without an explicit opt-in.
- **Checksum updates are verified** — the file is re-read after writing and the
  deployment fails if the new value did not persist. Line count and byte-delta
  are checked so nothing outside the checksum value can change.
- **No credentials in the frontend.** Passwords, keys and the installation code
  stay in the backend; the installation code is redacted from all logs.
- Bind to `localhost` only.

---

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest
```

97 tests covering service selection, checksum validation and injection
attempts, JAR/unit mapping, backup path generation, JAR and unit backups,
checksum replacement, the deployment stages, port detection and error handling.

**SSH and SFTP are fully mocked.** No test connects to the McKesson server and
no test performs a destructive operation. The suite runs anywhere, including on
a machine with no access to the app server.

---

## Project structure

```
mckesson-deployment-tool/
├── app/
│   ├── main.py                     FastAPI app, serves the dashboard
│   ├── config.py                   service registry + validators
│   ├── services/
│   │   ├── ssh_service.py
│   │   ├── sftp_service.py
│   │   ├── download_service.py
│   │   ├── backup_service.py
│   │   ├── checksum_service.py
│   │   ├── deployment_service.py
│   │   └── log_service.py
│   └── routes/
│       ├── deployment.py           REST API
│       └── websocket.py            /ws/logs
├── static/
│   ├── index.html
│   ├── css/style.css
│   └── js/app.js
├── tests/
├── temp/                           downloaded JARs + deployment log
├── .env.example
├── requirements.txt
└── README.md
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/services` | Configured services |
| GET | `/api/config` | Non-sensitive UI configuration |
| POST | `/api/checksum/verify` | Validate a pasted checksum |
| POST | `/api/deployment/validate` | Stage 1 checks |
| POST | `/api/deployment/download` | Download the selected JAR |
| POST | `/api/deployment/connect` | Open the SSH session |
| POST | `/api/deployment/backup` | Back up JAR + unit file |
| POST | `/api/deployment/upload` | Upload the selected JAR |
| POST | `/api/deployment/update-checksum` | Rewrite `APP_CHECKSUM` |
| POST | `/api/deployment/daemon-reload` | `systemctl daemon-reload` |
| POST | `/api/deployment/restart` | Restart the selected service |
| GET | `/api/deployment/service-status` | `systemctl is-active` + `status` |
| GET | `/api/deployment/status` | Current deployment state |
| POST | `/api/deployment/deploy` | Run the full pipeline |
| POST | `/api/deployment/find-port-process` | `lsof -i:<port>` (read-only) |
| POST | `/api/deployment/kill-process` | Kill a PID (**confirmation required**) |
| POST | `/api/deployment/restart-after-kill` | Restart + verify after a conflict |
| WS | `/ws/logs` | Stage events + live journal output |
