# AidenOps deployments

The `/aidenops` page. Separate from TX-PROJECTS in every respect: its own page,
its own API, its own code. Nothing here changes how the three Java services are
deployed.

Read this once before the first deployment. The rehearsal section is the part to
follow the first time.

---

## What is different from TX-PROJECTS

| | TX-PROJECTS | AidenOps |
|---|---|---|
| What arrives | one JAR, downloaded from the hub | one **zip** you copy onto the VDI |
| What you paste | the `APP_CHECKSUM` | the code for the whole archive |
| What is deployed | one JAR + a unit file edit | a **wheel** and/or a **UI bundle** |
| Database | none | migrations run on every backend start |
| On failure | you roll back by hand | the UI reverts itself; the backend hands you a runbook |

The important difference is the database. `config.yaml` has
`auto_migrate: true`, so **starting the backend runs Alembic**. A backend
deployment is a schema change whether or not anyone intended one, and
reinstalling the previous wheel does not undo a migration.

---

## Before you start

**1. The bundle.** The Aiden tool always publishes under one name:

```
opsBinaries.zip
```

Copy it into the tool's `incoming/` folder on the VDI. The page shows the file it
found with its size and the time it was copied — there is nothing to choose, and
no path to type.

The fixed name keeps the hub URL constant and matches the handover bundle
already on that server. It also means **the filename identifies nothing**: every
release is called this. Which is exactly why the code below is not a
double-check but the only way to tell one release from another.

**2. The code.** One SHA-256, shown by the Aiden tool in blocks of eight. Paste
it as shown; spaces and case do not matter. If it does not match, the deployment
stops there and nothing is sent - there is no override, because with a fixed
filename nothing else could tell you that you have the wrong release.

**3. `.env`.** The AidenOps defaults match this server and normally need no
change. See [Settings](#settings) if a path ever moves.

---

## The three stages

```
(1) Verify ─────────── (2) Check the server ─────────── (3) Deploy
```

### 1 · Verify — nothing touches the server

Two independent checks, both on the VDI:

- the archive's own hash against the code you pasted
- **every file inside it** against the `SHA256SUMS.txt` the archive carries

The first has no override. Nothing downstream would catch a wrong artifact: a
wrong wheel installs cleanly, starts cleanly, and `/health` returns 200 while
running the wrong code. That is not hypothetical — it is what happened on
17 August.

A release that fails is not kept. There is no path from a failed archive to a
deployment, and a previously good release is forgotten too, so a bad archive
cannot leave an older one quietly deployable.

### 2 · Check the server — read-only

- `config.yaml` is read and parsed here, and placeholders are reported.
  `database.*` and `jwt_secret` **block**; anything else warns, because most
  deployments never wire up ServiceNow or inbound email and a gate that nags
  gets switched off.
- the unit's current state, and whether `/health` answers now
- free space on **both** volumes — `/home/AidenAI` and `/var/www`

### 3 · Deploy

One button per part the release contains. They are independent: a UI-only
release is normal and common.

---

## Rehearse before you deploy

Same three phases as the Java flow.

**Phase 1 — offline.** `DRY_RUN=true`, `DRY_RUN_CONNECT=false`. Verification is
real (it is local), everything on the server is simulated. Proves the archive and
the code are good.

**Phase 2 — dry run with real SSH.** `DRY_RUN=true`, `DRY_RUN_CONNECT=true`.
Read-only commands really run, so this genuinely proves: the VDI can reach the
server, `day6sio` and sudo work, `config.yaml` parses, the disk has room, and the
migration plan is correct. Nothing is written, stopped or installed.

Look at the migration count here. If it says two additive migrations, that is
what Phase 3 will apply.

**Phase 3 — live.** `DRY_RUN=false`. Deploy the **UI first** if the release has
both: it is the reversible half, and it exercises the archive end to end before
anything irreversible runs.

---

## When it fails

### The UI reverts itself

```
extract → own → configure → relabel → swap → verify
                                                 └── fails → previous bundle restored
```

Static files and one move, so it reverts without asking and tells you it did.
Retry once you know why. Nothing is pruned on a failure — the previous and the
failed bundle are both kept.

### The backend stops and hands over

Everything that can refuse refuses **before** the service stops:

```
preflight · migrations · dependencies · dry-run resolve · dump + verify · wheel backup
────────────────── all of the above, service still running ──────────────────
stop · install dependencies · install wheel
────────── start: Alembic runs. recovery now needs the database ──────────
health · cleanup
```

Fail above the line and nothing has changed. Fail below it and the page shows a
runbook.

---

## The recovery runbook

Shown, never run. Two reasons:

- restoring **destroys everything written since the dump**, which is not a
  decision a script should make while nobody is watching
- the migration is often fine and the failure is elsewhere — a config typo, a
  missing dependency. Restoring would cost you data without fixing anything

```bash
systemctl stop aidenops.service

# DROP DATABASE refuses while anything is connected, and the pool's sessions
# outlive the unit stop.
sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
    WHERE datname='aidenops' AND pid <> pg_backend_pid();"

# Drop and recreate, not restore-over: a plain dump is full of CREATE TABLE and
# would hit "already exists" against a migrated schema, leaving it half old and
# half new.
sudo -u postgres psql -c "DROP DATABASE aidenops;"
sudo -u postgres psql -c "CREATE DATABASE aidenops OWNER aidap;"
gunzip -c /home/AidenAI/backups/db/aidenops-<stamp>.sql.gz | sudo -u postgres psql aidenops

/home/AidenAI/ops1/venv/bin/pip install --force-reinstall --no-deps \
    /home/AidenAI/backups/wheels/<previous>.whl
systemctl start aidenops.service
```

**When no dump was taken** — because the release had no pending migrations —
reinstalling the previous wheel is a complete rollback. The runbook says so
rather than offering a restore you do not need.

---

## What this tool will not do

Worth knowing before you need it:

- **it never restores the database.** `DROP DATABASE` appears nowhere in
  anything it executes
- **it never writes `config.yaml`.** That file holds the database password, the
  JWT secret and the Neo4j password. It is read and validated, never modified
- **it never auto-rolls-back the backend.** Reversing applied migrations is
  riskier than the failure
- **it never deploys an unverified archive**, and offers no override
- **it never starts without a verified rollback point** when migrations are
  pending

---

## Settings

Defaults match this server. Override in `.env` only if something moves.

| Setting | Default |
|---|---|
| `AIDENOPS_UNIT` | `aidenops.service` |
| `AIDENOPS_OPS_DIR` | `/home/AidenAI/ops1` |
| `AIDENOPS_VENV` | `/home/AidenAI/ops1/venv` |
| `AIDENOPS_STAGING_DIR` | `/home/AidenAI/ops1/staging` |
| `AIDENOPS_BACKUP_ROOT` | `/home/AidenAI/backups` |
| `AIDENOPS_WEB_ROOT` | `/var/www/aidenops` |
| `AIDENOPS_INCOMING_DIR` | `incoming` (relative to the tool) |
| `AIDENOPS_BUNDLE_NAME` | `opsBinaries.zip` |
| `AIDENOPS_HEALTH_URL` | `http://localhost:8000/health` |
| `AIDENOPS_UI_URL` | `http://localhost:8080/` |
| `AIDENOPS_HEALTH_TIMEOUT` | `600` |
| `AIDENOPS_HEALTH_INTERVAL` | `5` |
| `AIDENOPS_KEEP_PREVIOUS_DIST` | `1` |
| `AIDENOPS_KEEP_ARCHIVES` | `3` |
| `AIDENOPS_KEEP_DUMPS` | `3` |
| `AIDENOPS_DISK_MARGIN_MB` | `1024` |
| `AIDENOPS_NGINX_ERROR_LOG` | `/var/log/nginx/error.log` |
| `AIDENOPS_NGINX_ACCESS_LOG` | `/var/log/nginx/access.log` |

**`AIDENOPS_OPS_DIR` is `ops1`, not `ops`.** `/home/AidenAI/ops` is a stale
duplicate, and `aidenops-api.service` and `aidenops-ui.service` both point at it.
Restarting either reports success and changes nothing.

---

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| nginx returns **500**, not 403 | the parent directory is `0750` — `mkdir -p` under root's `umask 027` | the pipeline runs `chmod 755`; check it did |
| UI loads, nothing works | `runtime-config.js` has a blank `API_URL` | `aidenops-write-ui-config` did not run; the pipeline checks this and reverts |
| nginx cannot read the files | the tarball unpacks as **UID 4096** | `chown -R root:root` |
| denied despite correct permissions | SELinux label after a move | `restorecon -Rv /var/www/aidenops` |
| health never answers | Alembic still migrating — the port does not open until it finishes | wait; the poller allows 600s and a refused connection is expected |
| "no space" on a 50 GB volume | it is **`/var`** that is full, not `/home/AidenAI` | see below |
| dependency install fails | PyPI unreachable | only releases that change pins need it; otherwise no index access is required |
| `sudo` prompts or fails | `day6sio` needs a password; `timestamp_timeout=15` | `SUDO_PASSWORD` in `.env` |

### Live logs

The page follows three sources. `journalctl` because the app logs to stdout only
— there is no file log the way the Java services have one. And **both nginx
logs**, because both real UI failures on this server were diagnosed from
`error.log` and were invisible in the AidenOps journal.

---

## This server, as measured

Verified on 20 August 2026, not assumed.

```
/var                        8.5G used   1.5G free   86%
/home/AidenAI                19G used    32G free   37%

data_directory   /home/AidenAI/pgsql/16/data      5.2G
aidenops database                                  24 MB
```

**The `aidenops` database is 24 MB.** A gzipped dump of it is single-digit
megabytes, so three of them cost about 25 MB — not the ~140 MB quoted while this
was being designed. That figure came from the shipped demo dataset (2.36M rows),
which has never been loaded here. If it ever is, dumps grow accordingly and the
retention count matters much more.

### Two things on this server worth knowing - neither is a prerequisite

**No AidenOps deployment depends on either of these.** Every path this tool
writes to is on `/home/AidenAI`, which has 32 GB free. The only thing it puts on
`/var` is the UI bundle at roughly 6 MB, against 1.5 GB free - so `/var` being
tight does not threaten a deployment.

They are here because `/var` also holds nginx's logs and journald, and
`error.log` is where both real UI failures on this server were diagnosed. If
`/var` ever fills you lose that, which costs you diagnosis rather than uptime.

Housekeeping, in other words. Do it on a quiet afternoon or not at all.

**`/var/lib/pgsql` still holds 5.2 GB.** That is the *old* data directory, from
before PostgreSQL moved to `/home/AidenAI`. It is not in use —
`SHOW data_directory` confirms the live one — and it is 5.2 GB of the 8.5 GB
used on a volume at 86%. Removing it would take `/var` from 86% to roughly 34%.

Verify before touching it, and move rather than delete:

```bash
sudo -u postgres psql -c "SHOW data_directory;"       # confirm it is not this one
sudo systemctl status postgresql --no-pager | head -5
sudo lsof +D /var/lib/pgsql 2>/dev/null | head        # nothing should hold it open
# then, if all three agree:
sudo mv /var/lib/pgsql /home/AidenAI/pgsql-old-var
```

Moving it frees `/var` immediately and is reversible. Delete it later, once
something has run for a week without it.

**`/home/AidenAI/binaries` holds 6.7 GB**, mostly TX-PROJECTS JAR backups, which
nothing prunes. Not this tool's business and not urgent at 32 GB free — but it
shares a volume with the database now, so it is worth knowing:

```bash
du -sh /home/AidenAI/binaries/backups/
ls -1dt /home/AidenAI/binaries/backups/*/ | tail -n +4 | xargs -r rm -rf --
```

Also `/home/AidenAI/ops` (298 MB) is the stale duplicate directory, safe to
remove once you are satisfied nothing references it.
