# Setup on the McKesson VDI laptop

Step-by-step. Follow it in order — do not jump to a live deployment.

Full reference is in `README.md`; this file is the short path.

---

## Step 1 — Copy and unzip

Copy `mckesson-deployment-tool.zip` to the VDI, then:

```powershell
cd C:\Users\<you>\Tools          # any folder you can write to
Expand-Archive mckesson-deployment-tool.zip -DestinationPath .
cd mckesson-deployment-tool
```

## Step 2 — Check Python

```powershell
python --version
```

Needs **3.10 or newer**. If `python` is not recognised, try `py --version` and
use `py` in place of `python` below. If Python is not installed at all, install
it from the McKesson software portal before continuing.

## Step 3 — Create the virtual environment

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

This needs access to PyPI. **If it fails with a network/proxy error**, see
"No PyPI access" at the bottom of this file.

## Step 4 — Check `.env`

A `.env` is already included with the server details and the `day6sio`
password. Open it and confirm:

```powershell
notepad .env
```

| Setting | Should be |
|---|---|
| `APP_PORT` | `5002` |
| `DRY_RUN` | `true` |
| `DRY_RUN_CONNECT` | `false` |
| `SSH_USERNAME` | `day6sio` |
| `SSH_PASSWORD` | (filled in) |
| `SUDO_PASSWORD` | (filled in) |
| `SSH_HOST_KEY_POLICY` | `auto_add` |
| `INSTALLATION_CODE` | **empty — you must fill this in** |

For `INSTALLATION_CODE`, take the `code=` value from the download URL you use
today:

```
http://demo.aidap.aidendigital.com:8081/api/installation-hubs/path?filename=...&code=THIS-VALUE
```

## Step 5 — Start it

```powershell
.\venv\Scripts\python.exe run.py
```

Open <http://localhost:5002>. Leave this PowerShell window open — closing it
stops the app. To stop it, press `Ctrl+C`.

---

# Phase 1 — Offline dry run (nothing is touched)

`DRY_RUN=true`, `DRY_RUN_CONNECT=false` — this is how the zip ships.

1. Select **TX Test Management**.
2. Paste any 64-character SHA-256 checksum.
3. Click **Verify checksum** — the Deploy button unlocks.
4. Click **Deploy**.

All 10 stages should turn green and the log should read `Would download…`,
`Would back up…`, `Would update APP_CHECKSUM…`.

This proves the app and the pipeline work. It does not touch the server.

---

# Phase 2 — Dry run with real SSH (read-only)

Edit `.env`:

```ini
DRY_RUN=true
DRY_RUN_CONNECT=true
```

Restart the app (`Ctrl+C`, then `run.py` again) and deploy again.

**This is the important rehearsal.** It now really logs in to
`vm-mms-cims02.na.corp.mckesson.com`, so it proves:

- the VDI can reach the server on port 22
- the `day6sio` password works
- `sudo` works
- it can find `/etc/systemd/system/aiTXTestMgmt.service` and the existing JAR

Still nothing is modified, uploaded, restarted or killed.

Fix any error here before going further. Common ones are in the
Troubleshooting table in `README.md`.

Once this passes, set `SSH_HOST_KEY_POLICY=strict` in `.env` — the host key is
now cached and strict is the safer setting.

---

# Phase 3 — Real deployment

Edit `.env`:

```ini
DRY_RUN=false
```

Restart the app. A red **LIVE DEPLOYMENT** badge appears and Deploy now asks
for confirmation.

Do the first real deployment on the least critical service, and keep the backup
path shown in the log — rollback is:

```bash
sudo cp /home/AidenAI/binaries/backups/<date>/<jar> /home/AidenAI/binaries/
sudo cp /home/AidenAI/binaries/backups/<date>/<unit>.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart <unit>.service
```

---

# Everyday use after setup

1. Copy the checksum from the Aiden application (manually — the two tools are
   not connected).
2. On the VDI: `.\venv\Scripts\python.exe run.py`, open <http://localhost:5002>.
3. Select service → paste checksum → **Verify** → **Deploy**.
4. Watch the pipeline and the live logs.

---

# If a port is already in use

If `run.py` reports it cannot bind port 5002, change `APP_PORT` in `.env` to
e.g. `5003` and restart. Nothing else needs editing.

# No PyPI access

If `pip install` in Step 3 fails because the VDI cannot reach the internet, run
this **on the Aiden laptop** inside the project folder:

```powershell
python -m pip download -r requirements.txt -d vendor
```

Re-zip with the `vendor` folder included, then on the VDI:

```powershell
.\venv\Scripts\python.exe -m pip install --no-index --find-links=vendor -r requirements.txt
```

Note: `cryptography` and `bcrypt` are compiled packages, so the downloaded
files only work if the VDI runs the **same Python minor version** (e.g. both
3.12) on 64-bit Windows. Check `python --version` on both machines first.

# Verify the install without a server

```powershell
.\venv\Scripts\python.exe -m pytest
```

97 tests should pass. SSH and SFTP are mocked, so this works even with no
access to the app server and never touches it.
