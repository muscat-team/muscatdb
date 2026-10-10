# Deployment & Cluster Inventory

**Host facts probed live from the NIS gateway (`muscat-ut2`) on 2026-07-13; `muscat-ut4` re-probed after returning online.** A living, point-in-time reference for running muscat-db — single-host today, multi-host under the durable work queue in [MUSCATDB-LITE.md](MUSCATDB-LITE.md) §12. All host/load/network facts must be rechecked before any multi-host rollout.

---

## Host deployment (nginx + tmux)

muscat-db runs behind nginx (HTTP Basic Auth) reverse-proxying to uvicorn, inside tmux sessions. There are now **two live environments**, split by feature-landing order in issue #26. Production and staging use **dedicated code checkouts** under `$HOME/deploy/` so no deploy step ever resets the dev tree; the dev checkout (`$HOME/github/research/project/muscat-db`) is pure development.

| | Production | Staging |
|---|---|---|
| **Branch** | `main` | `test` |
| **Checkout** | `$HOME/deploy/main/app` | `$HOME/deploy/test/app` |
| **nginx port (public)** | `:8000` (Basic Auth) | `:8002` (Basic Auth) |
| **uvicorn port (loopback)** | `:8001` | `:8003` |
| **tmux session** | `muscatdbgui` | `muscatdb-test` |
| **`MUSCAT_DB_PATH`** | `$HOME/github/research/project/muscat-db/muscat.db` | `$HOME/deploy/test/muscat_test.db` (seeded nightly from prod) |
| **`MUSCAT_OBSLOG_DIR`** | `/ut2/muscat/obslog` | `$HOME/deploy/test/obslog` (its own copy, never the shared tree) |
| **`MUSCAT_PROSE_DIR`/`TIMER_DIR`/`TTV_DIR`** | `$HOME/ql/{prose,timer,harmonic}` | `$HOME/deploy/test/{prose,timer,harmonic}` |
| **`MUSCAT_MAX_FULL_JOBS`** | `1` | `0` (staging never runs full jobs) |
| **`MUSCAT_LCO_MONITOR_ENABLED`** | `1` | `0` |
| **`MUSCAT_LCO_ALLOW_SUBMIT`** | set | unset — can never book telescope time |

`deploy/pull-deploy.sh` deploys each branch to its own checkout, run from a cron entry per checkout (branch, tmux session and port are its three arguments). The script polls `git ls-remote` for a new SHA, resets to it, relaunches, then verifies `/healthz` before recording the deploy as good. The earlier `deploy.yml` push deploy was deleted in #133: GitHub-hosted runners cannot reach this host without the VPN, so it never worked, and the `DEPLOY_PATH_*`/`DEPLOY_TMUX_SESSION_*` actions variables it would have used are unused. Each checkout's `.env` pins the absolute paths above. Both launch uvicorn **without `--reload`** (dropped as part of the #26 host split — see the "Authentication boundary" note below for why `--reload` was harmful). `deploy/setup-nginx.sh` handles first-time nginx setup for production; staging's block lives at `/etc/nginx/sites-available/muscat-db-staging` (`:8002` → `:8003`).

**Cron status (2026-09-10):** both entries are installed and verified. Both went in on 2026-09-06, but staging's next real target hit a real outage the old `send-keys` relaunch could not recover from (fixed in #146 — see `notes/deploy-staging-plan.md` Gate F for the full incident and fix timeline); both entries were pulled by hand before production's poll could hit the same failure. With the fix merged to `test` (`257173d`), staging's entry alone was reinstalled and reverified against a real unattended tick on 2026-09-07. Once #146 released to `main` via #148 (`70de9ec`), production's entry was reinstalled on 2026-09-10 and its first unattended tick deployed `5923c3d -> 70de9ec` cleanly via `respawn-pane -k`, confirmed by `:8001/healthz` returning 200 right after. The Slack failure-alert webhook (`/etc/muscat-db/slack-webhook-url`) was installed on 2026-09-08, so a failed deploy now pages Slack in addition to logging `FAILED`. Gate F (#26, #131) is closed.

The README "Multi-User Deployment" section has the full walkthrough; the essentials:

```bash
# First-time nginx setup (as root)
sudo bash deploy/setup-nginx.sh

# Manage users (writes the htpasswd file + the SQLite users row)
sudo env "PATH=$PATH" uv run muscat-db htpasswd add <user>
uv run muscat-db htpasswd delete <user>
uv run muscat-db htpasswd list
# Make an existing user admin without touching their password
uv run muscat-db htpasswd promote <user>

# Restrict an LCO proposal's observations and grant users access (issue #144)
uv run muscat-db access restrict <proposal_id> [--description "..."]
uv run muscat-db access unrestrict <proposal_id>
uv run muscat-db access grant <user> <proposal_id>
uv run muscat-db access revoke <user> <proposal_id>
uv run muscat-db access list [--user <user>]
```

A restricted proposal is hidden from `/targets`, `/target`, project pages and
the target APIs for every viewer without a grant; admins
(`htpasswd promote <user>`) see everything, and a request with no authenticated
user is treated as having no grants. Restriction only covers frames whose
`proposal_id` is known, so `access restrict` warns while
`muscat-db backfill-propid` still has dates pending. Jobs, photometry,
transit-fit, TTV-fit and the `/inst/date/ccd` browser are not gated yet
(#144 PRs 5 and 6).

Connect to production via SSH tunnel: `ssh -L 8000:localhost:8000 <user>@muscat-ut2` → http://localhost:8000. Staging's public port is `:8002` (`ssh -L 8002:localhost:8002`).

### Authentication boundary and deployment verification

nginx authenticates browser users and forwards both `X-Forwarded-User` and a
private `X-MuSCAT-Proxy-Secret` header to uvicorn. In `--nginx` mode, the
application fails closed unless the request arrives from loopback with both
values valid. This prevents another account on the shared host from bypassing
HTTP Basic Auth by calling `127.0.0.1:8001` directly.

`deploy/setup-nginx.sh` creates the shared secret and the nginx include that
sets its header. The raw secret must be readable only by the account running
muscat-db:

```text
/etc/muscat-db/proxy-secret              0600 jerome:root
/etc/nginx/muscat-db-proxy-secret.conf   0600 root:root
```

Do not print, copy into documentation, or commit either secret value. Verify a
deployment without exposing it:

```bash
# Required ownership and mode for the raw application secret
stat -c '%A %a %U:%G %n' /etc/muscat-db/proxy-secret

# Configuration is valid; this does not reload nginx
sudo nginx -t

# Expected: 401 (nginx requires HTTP Basic Auth)
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/

# Expected: 200 (the deliberately public liveness probe)
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/healthz

# Expected: 401 (direct uvicorn access fails closed)
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/
```

Changing the secret file's owner/mode and running `nginx -t` do not stop
nginx, uvicorn, or science-pipeline processes, so they do not interrupt running
jobs. A muscat-db restart is a separate operation. Restart only in the correct
tmux session (`muscatdbgui` for production, `muscatdb-test` for staging), after
checking for active photometry/transit jobs. `--reload` is **not** used in
production or staging: the deployment launches uvicorn without it (see issue
#26), because a reload mid-`ingest_date` rolls back a whole night's ingest and
because it lets a dev-tree branch switch restart the live server. Deploys are
driven by `deploy/pull-deploy.sh` from cron, which restarts the matching session
after a `git reset --hard` in the dedicated checkout; since `--reload` is gone,
HTML/JavaScript changes require that deploy restart before they are live.

---

## Cluster inventory

Hosts derive from `/etc/hosts` on the gateway. Two subnets: `157.82.46.x` (West) and `157.82.29.x` (East).

| Host | IP | CPU Model | Phys / Log cores | RAM | OS / Python | Load @ probe | Status |
|------|----|-----------|-----------------|----|-------------|--------------|--------|
| **muscat-ut2** | 157.82.46.83 | Xeon Gold 5120 @2.2GHz (1 socket) | 14 / 28 | 44 GiB (+119 GiB swap) | Ubuntu 26.04 / 3.12.8 | low | **up** — NIS gateway, web GUI (tmux `muscatdbgui`), NFS server, NTP |
| **muscat-ut3** | 157.82.46.17 | 2× EPYC 7542 32C | 64 / 128 | 125 GiB | Ubuntu 22.04.5 / 3.10.12 | 2.4 (idle-ish) | **up** — `/raid_ut3` exported |
| **muscat-ut4** | 157.82.46.41 | Core i9-10940X @3.3GHz (1 socket) | 14 / 28 | 125 GiB (+127 GiB swap) | Ubuntu 22.04.5 / 3.10.12 | 0.12 | **up** — `/ut2` NFS, science envs, repo, DB, and NTP verified |
| **muscat-ut5** | 157.82.29.73 | 2× Xeon Gold 6338 32C @2.0GHz | 64 / 128 | 251 GiB | Ubuntu 22.04.5 / 3.10.12 | **~215 (saturated)** | **up but heavily loaded** — `/raid_ut5` exported |
| **muscat-ut6** | 157.82.29.74 | 2× Xeon Gold 6338 32C @2.0GHz | 64 / 128 | 251 GiB | Ubuntu 22.04.5 / 3.10.12 | 0.15 (idle) | **up, most available** |
| **muscat-ut7** | 157.82.46.82 | EPYC 9555 64C (1 socket, no SMT) | 64 / 64 | 246 GiB | Ubuntu 26.04 / 3.14.4 | **~225 (saturated)** | **up but heavily loaded** |

**Note on core counts:**
- ut2 (gateway) = 28 logical threads.
- ut3 is a bonus 128-logical host.
- ut4 = 14 physical / 28 logical threads with 125 GiB RAM; suitable for trial, light, or overflow work after a stability check.
- ut5 & ut6 (the "120-core" workhorses) = 128 logical threads each.
- ut7 = 64 physical cores (no hyper-threading).

---

## Software state

- Python is available everywhere; versions vary by OS (see table above).
- The original cluster probe found **no PostgreSQL server or `psycopg`** on any host. PostgreSQL 18 is now installed on ut2 (see [PostgreSQL control plane on ut2](#postgresql-control-plane-on-ut2)); `psycopg` is still absent on ut3–ut7. These are the `[cluster]` prerequisite for the multi-host control plane (§12). **Redis and Celery are not used** by the §12 design.

### Present on ut2 only
- **`uv` package manager** — used by the web app (`uv run`).
- On workers, either install `uv` per host or run worker entrypoints via the shared conda environments.

### Required before multi-host rollout
- ~~Install PostgreSQL on ut2~~ — done, see below. Still open: `psycopg` in the selected worker environment(s) (`muscatdb[cluster]`).
- Verify clock sync (NTP) across all participating hosts (lease/heartbeat expiry depends on it).

---

## PostgreSQL control plane on ut2

Installed 2026-10-10 from the Ubuntu 26.04 archive (the only candidate is PostgreSQL **18**; there is no `postgresql-16`). Local-only: it listens on `127.0.0.1:5432` and nothing reaches it from the LAN yet.

### Install

```bash
sudo apt update && sudo apt install -y postgresql postgresql-contrib
pg_lsclusters                      # expect: 18  main  5432  online
pg_isready
```

The package creates and enables the `main` cluster (data dir `/var/lib/postgresql/18/main`, on the local `/` disk, not NFS). `systemctl status postgresql` shows `active (exited)`: that is the wrapper unit, the real service is `postgresql@18-main`.

Never put the data directory on NFS (locking and fsync semantics; the same reason SQLite is being replaced).

### Role and databases

```bash
sudo -u postgres createuser --pwprompt muscatdb
sudo -u postgres createdb -O muscatdb muscatdb_control   # control plane for the app
sudo -u postgres createdb -O muscatdb muscatdb_test      # pytest only, see warning below
```

Keep the DSN out of the repo and out of shell history, in a file only the deploy user can read:

```zsh
mkdir -p ~/.config/muscatdb && umask 077
read -rs "PW?password: " && echo
printf 'export MUSCAT_POSTGRES_DSN=postgresql://muscatdb:%s@localhost:5432/muscatdb_control\n' "$PW" \
  > ~/.config/muscatdb/pg.env; unset PW
```

(In zsh `read -p` means coprocess; use the `"VAR?prompt"` form. URL-encode `@ : /` in the password.)

### Environment variables

| Variable | Value | Notes |
|---|---|---|
| `MUSCAT_CONTROL_PLANE` | `postgres` (default `sqlite`) | Resolved once on the first `get_job_store()` call, then cached: restart to change it. |
| `MUSCAT_POSTGRES_DSN` | `postgresql://muscatdb:<pw>@localhost:5432/muscatdb_control` | Required when the above is `postgres`. |
| `MUSCAT_WORKER_MAX_SLOTS` | `1` on workers; `0` on a web instance that should only queue | Concurrent full jobs per pipeline per host. `0` makes `claim_slot` unsatisfiable, so full runs of every pipeline stay `pending` for a worker. Set from `deploy/workers.toml` (`max_slots`, `[web] queue_only`). |
| `MUSCAT_JOB_MAX_THREADS` | `8` on workers | Thread cap handed to each job. |
| `MUSCAT_JOB_NOTIFY` | `1` | Wake an idle worker through Postgres `LISTEN`/`NOTIFY` instead of waiting out the poll interval. |

`psycopg` comes from the `cluster` extra (`psycopg[binary,pool]`). To use it without touching a server's `.venv`: `uv run --with 'psycopg[binary,pool]>=3.2' ...`.

### Switching an instance over

1. Confirm nothing is queued or running in the SQLite jobs table (`state` of `pending`/`running`). Job history is **not** migrated; the Postgres tables start empty.
2. Identify the pane that hosts the process before sending it anything. Trace the process to its tmux pane (`ps -o ppid=` up to the shell, then `tmux list-panes -a -F '#{session_name} #{pane_pid}'`). Do not assume from the session name: on 2026-10-10 `muscatdbgui` hosted production `:8001`, not the dev server, and an interrupt sent there took production down.
3. Restart in that pane with the variables set, e.g. for the dev server:

   ```bash
   source ~/.config/muscatdb/pg.env && export MUSCAT_CONTROL_PLANE=postgres \
     && uv run muscat-db restart --port=8888 --reload
   ```
4. `uv run` and `--reload` only restart on Python changes; templates and JavaScript need a manual restart (see CLAUDE.md).

### Warning: the Postgres test suite truncates its tables

`tests/test_job_store_postgres.py` runs `TRUNCATE jobs, job_concurrency_slots` in its fixture. Point it at a database nothing else uses:

```bash
source ~/.config/muscatdb/pg.env
export MUSCAT_POSTGRES_DSN="${MUSCAT_POSTGRES_DSN%/*}/muscatdb_test"
uv run --with 'psycopg[binary,pool]>=3.2' pytest tests/test_job_store_postgres.py -q
```

Do not run it against the database a live instance is using.

### Verification record (2026-10-10, ut2)

- Server: PostgreSQL 18.6, role `muscatdb`, schema initialised by `PostgresJobStore` (`jobs`, `job_concurrency_slots`).
- `tests/test_job_store_postgres.py`: 51 passed against `muscatdb_control` (while empty) and `muscatdb_test`.
- Live dev server (`:8888`, `MUSCAT_CONTROL_PLANE=postgres`): a `transit_fit` run (sinistro / 230618 / `tfn-tel14-full_frame-default`) went `running` → `done`, `returncode` 0, 168 s, `attempts` 0, heartbeat refreshed throughout, and `job_concurrency_slots` returned to 0 rows.

### Done

- [x] PostgreSQL 18 installed on ut2 from the Ubuntu 26.04 archive; cluster `main` online and enabled at boot.
- [x] Initially bound to `127.0.0.1:5432` only; since the multi-host test it also listens on `157.82.46.83`, with `pg_hba.conf` and `ufw` entries per worker host (below).
- [x] Role `muscatdb` and databases `muscatdb_control` and `muscatdb_test` created.
- [x] DSN kept in `~/.config/muscatdb/pg.env` (mode 600), outside the repo.
- [x] `PostgresJobStore` connects and initialises its schema (`jobs`, `job_concurrency_slots`).
- [x] `tests/test_job_store_postgres.py`: 51 passed against the live server.
- [x] Dev server (`:8888`) switched to `MUSCAT_CONTROL_PLANE=postgres` against `muscatdb_test`, with no queued or running jobs at switch time.
- [x] Live `transit_fit` run on Postgres: `running` → `done`, rc 0, heartbeat refreshed, `attempts` 0, concurrency slot released.
- [x] Postgres opened to **ut3 only** (`157.82.46.17/32`, database `muscatdb_test`, role `muscatdb`); `muscatdb_control` is refused from ut3 (`no pg_hba.conf entry`).
- [x] The 51 job-store tests pass **from ut3 over the network** against ut2's Postgres.
- [x] A standalone `muscat-db worker --pipeline transit_fit` on ut3 claimed and ran two real full fits end to end (`owner=worker`, `instance_id=muscat-ut3:…`, rc 0, `attempts` 0), with slot accounting per host and release at the end.
- [x] `ttv_fit` and `photometry` (full run, sinistro / 230618) each ran to `done`, rc 0, on a ut3 worker claimed from the queue; all three pipelines have run on Postgres.
- [x] Postgres opened to **ut5** (`157.82.29.73/32`, `muscatdb_test` only; `muscatdb_control` refused) and `psycopg` confirmed there; three workers (`transit_fit`, `ttv_fit`, `photometry`) are connected from ut5 (3 `LISTEN` connections).
- [x] Worker fleet declared in `deploy/workers.toml` and run as systemd user units (muscat-team/muscatdb#219): unit smoke-tested on ut5 (connected, host guard skips it on ut3, `down` stops and disables it).
- [x] Orphan reclaim (#174) verified once on ut3 (see below).

### Multi-host smoke test (ut2 web → ut3 worker), 2026-10-10

Network exposure for ut3 only (run in a real terminal on ut2; `sudo` has no tty through the agent):

```bash
sudo -u postgres psql -c "ALTER SYSTEM SET listen_addresses = 'localhost,157.82.46.83';"
sudo cp -p /etc/postgresql/18/main/pg_hba.conf /etc/postgresql/18/main/pg_hba.conf.bak-20261010
echo "host  muscatdb_test  muscatdb  157.82.46.17/32  scram-sha-256" | sudo tee -a /etc/postgresql/18/main/pg_hba.conf
sudo ufw allow from 157.82.46.17 to any port 5432 proto tcp comment 'postgres: ut3 worker test'
sudo systemctl restart postgresql@18-main
pg_isready -h 157.82.46.83
```

`ufw` is active on ut2: without the allow rule the connection times out silently. `pg_hba.conf` is readable only by `postgres`.

Workers on ut3 and ut5 are declared in `deploy/workers.toml` and run as systemd user units by `deploy/workers.py` (muscat-team/muscatdb#219; the commands below exist once that PR is merged). The password stays in `pg.env`, read at start by `deploy/worker-run.sh`; only the host and database are swapped to ut2's:

```toml
[defaults]
pg_host   = "157.82.46.83"      # ut2
database  = "muscatdb_test"     # muscatdb_control for the real control plane
max_slots = 1                   # MUSCAT_WORKER_MAX_SLOTS, per pipeline per host
[web]
queue_only = true               # web app queues full runs for the workers
[hosts.ut3]
ssh = "muscat-ut3"
pipelines = ["transit_fit", "ttv_fit", "photometry"]
[hosts.ut5]
ssh = "muscat-ut5"
pipelines = ["transit_fit", "ttv_fit", "photometry"]
```

```bash
.venv/bin/python deploy/workers.py --dry-run up   # read-only probes; changes nothing
.venv/bin/python deploy/workers.py up ut5         # one host; workers start one at a time
.venv/bin/python deploy/workers.py status
.venv/bin/python deploy/workers.py down ut5       # refuses while jobs run there; --force to override
eval "$(.venv/bin/python deploy/workers.py web-env)"   # in the shell that starts the web app
```

Run it from ut2 (it needs Python >= 3.11; the workers' system python is 3.10). Once per host, `loginctl enable-linger $USER` so the units survive the last logout; `up` warns when it is off.

Adding another host takes two steps on ut2 that need `sudo` in a real terminal, then one command:

```bash
sudo cp -p /etc/postgresql/18/main/pg_hba.conf /etc/postgresql/18/main/pg_hba.conf.bak-$(date +%Y%m%d)
echo "host  muscatdb_test  muscatdb  <host-ip>/32  scram-sha-256" | sudo tee -a /etc/postgresql/18/main/pg_hba.conf
sudo ufw allow from <host-ip> to any port 5432 proto tcp comment 'postgres: <host> worker'
sudo systemctl reload postgresql@18-main     # reload is enough for pg_hba.conf
# then add [hosts.<name>] to deploy/workers.toml and run: workers.py up <name>
```

ut5 (`157.82.29.73`, a different subnet from ut3) was added this way on 2026-10-10: the `muscatdb` role authenticates to `muscatdb_test` over scram-sha-256 and `muscatdb_control` is refused. Port 5432 was already reachable from ut5 at the TCP level before the new rule, so something broader than the per-host rule is allowing it through `ufw`; check `sudo ufw status numbered`.

Making a job run on a worker: the web process launches a full run itself whenever a slot is free and the worker only claims `pending` rows, so the ut2 web process wins the race for the slot (it drains the queue in the same step that releases it). Set `MUSCAT_WORKER_MAX_SLOTS=0` on the web instance: its `claim_slot` can never satisfy the host cap, full runs go `pending`, and the ut3 worker claims them. Test runs are not slot-gated and still run on ut2.

Observed: with the cap at 0 on the dev server, two full runs submitted from the UI both ran on ut3 (`owner=worker`), the second queued behind the first on the single slot, and `job_concurrency_slots` was empty at the end.

`MUSCAT_WORKER_MAX_SLOTS=0` applies to every pipeline: with it, a full photometry or TTV run also stays `pending` until a worker for that pipeline exists. A `ttv_fit` and a `photometry` run submitted from the UI sat `pending` with no owner until a worker for each pipeline was started; both then ran on ut3 and finished `done`, rc 0 (so all three pipelines have now run on Postgres).

Known limitations seen: the worker process logs only its startup line (the fit's own log is under the run directory on NFS); cancelling a ut3 job from the web UI does not work (process-local registry, documented in `worker.py`).

Two bugs found while doing this, both backend-independent and fixed in muscat-team/muscatdb#218 (not merged; the dev server still runs the old code):

- A queued full run was overwritten by the finished test run it replaces: both share one job key, and the next `sync_jobs` pass wrote the stale in-memory job's `done` over the `pending` row, so no full run ever started and the page showed the previous test output.
- `/jobs` re-run of a run without an explicit name forked a new `run_id` (and a second row): it copied the `run_name`→`run_id` display fallback back into the options, so the site/telescope/mode prefix was applied twice.

### Orphan reclaim (#174) on ut3, 2026-10-10

- [x] A running full fit on ut3 was orphaned (`kill -9` of the worker, then of the fit's process group, identified by pid so no other ut3 job was touched). The row stayed `running` with a stale heartbeat. After a new worker started, it logged `orphaned with no evidence of completion; retrying (attempt 1)`, the row went to `attempts=1` under the new `instance_id`, a fresh `timer-fit` was launched, and the run finished `done` (rc 0) with one row and no slots held.

Findings:

- [x] Only a worker pass reclaims a worker-owned row. The web process never reconciles `owner=worker` rows (role separation), so while no worker is running the job sits `running` with a stale heartbeat (it was 200 s past `MUSCAT_JOB_HEARTBEAT_STALE_S=30` with no reclaim) and the UI keeps showing it as running. Keep a worker supervised (systemd `Restart=`), otherwise orphans wait for a manual restart.
- [x] A worker started as a tmux window's own command takes the pane with it when it dies, and a respawned pane has no `uv` on `PATH` (`exec: uv: not found`, status 127). Run workers as the systemd user units from `deploy/workers.py` (`Restart=on-failure`, no `uv` needed: the unit execs the venv's `muscat-db`) instead of tmux windows.

### Concurrent worker start deadlocks in the schema check, 2026-10-10

Starting the three ut5 workers at the same moment made two of them fail their first connection with `DeadlockDetected` in `_ensure_pg_jobs_schema` (`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS …` from several processes). They survived: the store is created lazily and the next pass succeeded, and all three held a `LISTEN` connection afterwards. What was skipped was the one-time "resolve stale `cancelling` rows" at startup (logged as `could not resolve stale 'cancelling' job rows`), and the photometry worker also logged one failed reconciliation pass.

- [x] Worked around: `workers.py up` starts one host's workers one at a time (`stagger_s`).
- [ ] Not fixed: serialize `_ensure_pg_jobs_schema` with an advisory lock (the pattern `claim_slot` already uses). It will recur on a coordinated restart of several hosts or of the web app and workers together.

### Not covered yet

- [ ] Orphan reclaim (#174), retry limit: kill the same job repeatedly and confirm it errors after `MUSCAT_JOB_MAX_RECONCILE_ATTEMPTS` (a single kill and requeue was verified, see above).
- [ ] `MUSCAT_JOB_NOTIFY=1` wake-up latency over real Postgres `LISTEN`/`NOTIFY` (the flag was on for the ut3 worker, but latency was not measured).
- [ ] #209 access gating against a Postgres-backed job list.
- [ ] A job actually claimed and run **on ut5** (its three workers are connected and idle; every job so far ran on ut3 or ut2).
- [ ] Network exposure beyond ut3 and ut5: `pg_hba.conf` and `ufw` entries for ut4, ut6 and ut7, and a least-privilege worker role (the current `muscatdb` role owns both databases).
- [ ] `psycopg` in the worker environments of ut4, ut6 and ut7 (ut3 and ut5 use the shared repo venv).
- [ ] Move the running tmux workers on ut3 and ut5 to the systemd units (muscat-team/muscatdb#219, not merged), and `loginctl enable-linger` on both (currently `Linger=no`, so units would stop at the last logout). The unit was smoke-tested on ut5 with one extra `ttv_fit` worker; the existing workers were not migrated.
- [ ] Merge muscat-team/muscatdb#218 and restart the dev server: until then a full run queued after a test run on the same key can be overwritten, and `/jobs` re-run of an unnamed run forks a row.
- [ ] Reboot test of the units (needs linger).
- [ ] Fix the concurrent schema-check deadlock (see the section above).
- [ ] Real host routing: today it is the `MUSCAT_WORKER_MAX_SLOTS=0` workaround, not a queue that targets a host.
- [ ] Cancelling a job that runs on another host.
- [ ] Backups: `muscat.db` has a daily backup, the Postgres control plane has none.
- [ ] Decide whether SQLite job history should be migrated or left behind.
- [ ] Switch production (`:8001`) and the test deploy (`:8003`) only after the above and a release.

---

## Shared filesystem

The **linchpin assumption** for multi-host workers: `/ut2` and everything under it resolves identically on every host (the §12 shared-mount invariant).

### How it works
- On **ut2**: `/ut2` is a symlink to `/raid_ut2/home`. The `/raid_ut2` volume is a local ext4 filesystem on ut2.
- ut2 exports `/raid_ut2` via NFSv4 to all `muscat-ut*` hosts with `rw,sync,no_subtree_check,no_root_squash`.
- Worker hosts reach it via autofs (`/etc/auto.ut2`) which mounts `muscat-ut2:/raid_ut2` at `/mnt_ut2/raid_ut2`.
- The symlink `/ut2 → /raid_ut2/home` resolves to the same physical files on every host.

### Verified accessible on workers
- Conda environments: `$HOME/miniconda3/envs/prose`, `$HOME/miniconda3/envs/timer`, `$HOME/miniconda3/envs/harmonic`
- External tools: `$HOME/github/research/project/ext_tools/{prose2,timer,harmonic}`
  (see [Engine checkouts](#engine-checkouts) for the remote each must track)
- Repo: `$HOME/github/research/project/muscat-db/`
- Database: `$HOME/github/research/project/muscat-db/muscat.db` (size varies with each nightly `build-db`; ~1.6 GiB as of 2026-09-01 — the previously-recorded 3,066,445,824 bytes is stale, consistent with `build_db` writing a fresh compact file rather than data loss)

### Shared-input paths must be pinned, not left `$HOME`-relative

`MUSCAT_OBSLOG_DIR` (and `MUSCAT_DATA_DIR`) are a different category from the
four paths above: they name data populated by *whatever writes the obslog CSVs
/ raw FITS*, not by the muscat-db deployment account. On ut2 that's a separate,
long-standing `muscat` account running its own `auto_mkobslog.pl`, unrelated to
muscat-db. `instruments.py`'s in-code default for `MUSCAT_OBSLOG_DIR` still
falls back to `Path.home()/muscat/obslog` when unset, so any command run as an
account other than `muscat` (i.e. every manual/GUI invocation here, since the
deployment runs as jerome) silently resolved a different, near-empty tree with
no error — see #71. The daily cron already worked around this by exporting
`MUSCAT_OBSLOG_DIR=/ut2/muscat/obslog` itself (see the [Cron
section](../README.md#cron-daily) of the README), but that override never
covered manual CLI runs or the `muscatdbgui` session, which only read `.env`.
The cron now runs from the **production checkout** (`$HOME/deploy/main/app`),
not the dev tree, so it has no dependency on a dev branch switch (see issue
#26): it does the nightly prod `scan-yesterday` + `build-db`, then reseeds
`muscat_test.db` from prod via SQLite's backup API and runs an isolated staging
`scan-yesterday` against staging's own `MUSCAT_OBSLOG_DIR`. The README's [Cron
section](../README.md#cron-daily) gives the generic, host-agnostic three-step
form for anyone else deploying this repo; the literal line actually installed
on this host, staging refresh included, is:

```
MUSCAT_OBSLOG_DIR=/ut2/muscat/obslog
MUSCATDB_ROOT=/ut2/jerome/deploy/main/app
MUSCAT_TEST_ROOT=/ut2/jerome/deploy/test/app
MUSCAT_TEST_DB=/ut2/jerome/deploy/test/muscat_test.db
MUSCAT_TEST_OBSLOG_DIR=/ut2/jerome/deploy/test/obslog
30 17 * * * cd $MUSCATDB_ROOT && bash scripts/download_catalogs.sh >> $MUSCATDB_ROOT/logs/download_catalogs.log 2>&1 && /ut2/jerome/.local/bin/uv run muscat-db scan-yesterday >> $MUSCATDB_ROOT/logs/scan.log 2>&1 && /ut2/jerome/.local/bin/uv run muscat-db build-db >> $MUSCATDB_ROOT/logs/build-db.log 2>&1 && /usr/bin/python3 -c "import sqlite3; s=sqlite3.connect('file:/ut2/jerome/github/research/project/muscat-db/muscat.db?mode=ro',uri=True); d=sqlite3.connect('$MUSCAT_TEST_DB'); s.backup(d); d.close(); s.close()" >> $MUSCAT_TEST_ROOT/logs/staging-refresh.log 2>&1 && cd $MUSCAT_TEST_ROOT && MUSCAT_OBSLOG_DIR=$MUSCAT_TEST_OBSLOG_DIR /ut2/jerome/.local/bin/uv run muscat-db scan-yesterday >> $MUSCAT_TEST_ROOT/logs/scan.log 2>&1
```

`cronjob.txt` itself is not tracked (it was a root-level file that only ever
made sense as this host's literal crontab, never as example config); this
block is now the record of what the host actually runs, kept alongside the
other deliberately-recorded host state in this file. Update it here, not in a
tracked `cronjob.txt`, the next time the crontab changes.

**Rule:** pin `MUSCAT_OBSLOG_DIR` explicitly in `.env`, on a single host or
many — never rely on its `$HOME`-relative in-code default in production.
`MUSCAT_DATA_DIR`'s in-code default (`/data`) is not `$HOME`-relative and
needs no pin as-is; pin it too only if it's ever pointed somewhere else. The
startup log (`[startup] env config:`) now prints the resolved value whenever
a variable is using its in-code default, so an unpinned shared-input path is
visible instead of silent.

### Engine checkouts

Each pipeline engine must track its owner's repository. Forks exist, so a checkout
pointing somewhere else is not obvious from the directory name alone:

| Engine | Required remote |
|---|---|
| prose2 | `github.com/jpdeleon/prose2` |
| timer | `github.com/john-livingston/timer` |
| harmonic | `github.com/john-livingston/harmonic` |

The conda environments supply each engine's dependencies; the code itself comes from
these checkouts through editable installs, so the ref a checkout sits on is the
version of the engine that runs. `timer` and `harmonic` record `editable: true` in
their `direct_url.json` pointing back here, and `prose` imports from
`ext_tools/prose2/prose/__init__.py`. See #79 for what is missing about the
environments themselves.

A remote alone does not identify what runs, because every engine is checked out on
a branch rather than at a release, and none of those branch names exists on the
remote it tracks. Record the ref and commit alongside the remote, and re-record them
whenever an engine is updated on the host. A branch name is not enough on its own:
it does not let anyone else reproduce a run, and a commit that was never pushed
cannot be recovered at all if this host is lost.

The state observed at any given time is tracked in the issue that covers it rather
than here, so this section does not go stale: see #77.

Verify before trusting a pipeline result, since a fork can be behind upstream or
carry a patch that exists in no release:

```bash
for e in prose2 timer harmonic; do
  d="$HOME/github/research/project/ext_tools/$e"
  printf '%-9s %s @ %s %s\n' "$e" "$(git -C "$d" remote get-url origin)" \
    "$(git -C "$d" rev-parse --abbrev-ref HEAD)" "$(git -C "$d" rev-parse --short HEAD)"
  git -C "$d" status --short | head -3
done
```

A dirty working tree here is a defect, not a convenience. An engine patched only on
this host is reverted by the next `git pull`, and until then both repositories behave
differently from their source. Commit and push the fix, or revert it and adapt
muscat-db instead. `AGENTS.md` has the rule for deciding which.

**Production should not run out of these `-e` checkouts at all — tracked in #101.**
The editable installs above are correct for interactive engine development (that is
what `-e` is for), but a job subprocess launched against the same env a `pip install
-e` was just run into picks up the next uncommitted save with no error and no
deploy step, which is how #77/#84 happened. The decided approach (see #26's
comment thread, which found and fixed the same problem one layer up for the
muscat-db checkout itself): separate conda envs for production from the ones used
for dev — e.g. `prose-prod`/`timer-prod`/`harmonic-prod`, each `pip install
git+https://github.com/<owner>/<repo>@<sha>` (non-editable) rather than `-e`, left
alongside the existing `prose`/`timer`/`harmonic` dev envs untouched. Point
`MUSCAT_PROSE_CONDA_ENV`/`MUSCAT_TIMER_CONDA_ENV`/`MUSCAT_HARMONIC_CONDA_ENV` at the
`-prod` envs in production's `.env` once they exist; record each engine's pinned
remote+SHA here, next to the dev-checkout table above, when that lands. Not done as
of this writing — see #101 for status. muscat-db's own launch path no longer needs
an engine checkout on `sys.path` via `cwd` to find the code (photometry now resolves
the `photometry` console script from the conda env directly, matching how transit-fit
and TTV already resolved `timer-fit`/`harmonic`), so the env's installed package is
authoritative for all three pipelines once the `-prod` envs exist.

### Cross-mounted raids
Three NFS servers auto-mount each other's storage:
- ut2 exports `/raid_ut2`
- ut3 exports `/raid_ut3` (mounted on ut2/ut6 at `/mnt_ut3/raid_ut3`)
- ut5 exports `/raid_ut5` (mounted on ut2/ut3/ut6 at `/mnt_ut5/raid_ut5`)

### Implication
The §12 shared-path precondition is **already satisfied for ut2/ut3/ut4/ut6**: the conda environments, external tools, repository, and (read-only) catalog database resolve through `/ut2`. Any new worker host needs the same autofs mount and NIS domain first. Logs and science outputs on this shared mount are what let the web host tail worker logs for SSE.

---

## Recommended worker role assignment (§12 durable queue)

Under §12 every worker runs the same `muscatdb worker --pipeline <name>` loop, pulling jobs from the durable queue (SKIP-LOCKED claim + lease). **No broker.** The **control plane** (PostgreSQL, `[cluster]`) lives on ut2.

### PostgreSQL control plane → **ut2**
- Always-on gateway co-located with the FastAPI web app (minimizes web↔DB latency).
- Lightweight; 44 GiB is ample.
- Bind to the LAN interface; grant workers a **least-privilege** role; firewall TCP 5432 to participating `muscat-ut*` hosts only (both subnets, 46.x ↔ 29.x).

### Photometry workers (`prose` env, high FITS I/O) → **ut6** (primary), **ut3** (secondary), **ut4** (trial/overflow)
- Large RAM + currently available (ut6 load 0.15, ut3 load 2.4); direct NFS access to raw data.
- Per-pipeline concurrency 1 for full reductions (each prose run already fans out internally via `SequenceParallel`).
- Promote ut4 only after a full-reduction smoke test and an availability burn-in.

### Transit-fit / TTV workers (`timer`/`harmonic`, CPU-heavy MCMC) → **ut6**, **ut3** (primary); **ut4**, **ut5/ut7** (capacity-gated)
- High core counts suit MCMC sampling.
- ut5 and ut7 are **currently saturated** by other users (load ~215–225); do not blind-schedule heavy work there.
- Prefer ut6/ut3; use ut4 for tests or moderate overflow; use ut5/ut7 only when live load permits. Apply `nice`/cgroup caps on shared hosts.

### Newly restored host guardrail
- **ut4** passed SSH, `/ut2` autofs, NTP, repo, DB, and science-env checks after returning online, but its uptime was only ~7 minutes at probe. Require a smoke test and stability window before production full-pipeline work.

---

## Operational risks & prerequisites

### 1. Concurrent job-state writes on a shared filesystem
**Risk:** SQLite's file-locking is unreliable over NFS; multiple workers writing the jobs table risk corruption / lost updates.
**§12 resolution:** the mutable **control plane moves to PostgreSQL** (`[cluster]`) — workers write job state transactionally to Postgres, never to SQLite over NFS. The catalog `muscat.db` stays SQLite but is derived, local to ut2, and read-only to workers. No single-writer callback is needed.

### 2. `OMP_NUM_THREADS=100` is set in the environment
**Risk:** makes `nproc` report 100 on a 28-thread machine; a worker oversubscribes ~100× if unset.
**Mitigation:** in each **worker systemd unit**, pin `OMP_NUM_THREADS` / `MKL_NUM_THREADS` / `OPENBLAS_NUM_THREADS` to the host's logical-core budget (e.g. `14` on ut2).

### 3. OS/Python heterogeneity
**Risk:** Ubuntu 22.04 (py3.10) vs 26.04 (py3.12–3.14) differ in glibc; a compiled venv is not portable across major OS versions.
**Mitigation (preferred):** run workers inside the NFS-shared conda envs (`prose`/`timer`/`harmonic`), known to work across ut2/ut3/ut6. Install `psycopg` into a dedicated shared conda env on `/ut2`. **Alternative:** containerize workers with OS-pinned images.

### 4. `uv` missing on workers
**Risk:** the web app uses `uv run`; workers don't have `uv`.
**Mitigation:** install `uv` per host, or run worker entrypoints via conda directly.

### 5. PostgreSQL reachability
**Risk:** firewall/segmentation may block TCP 5432 from workers to ut2.
**Mitigation:** verify ut2 accepts inbound 5432 from `157.82.46.x` and `157.82.29.x`; test `psql -h 157.82.46.83 -p 5432` from each worker; add firewall exceptions for `muscat-ut*` → ut2:5432.

### 6. Existing load on ut5 and ut7
**Risk:** both saturated (load ~215–225); heavy transit-fit work contends with other users.
**Mitigation:** load-aware routing (overflow to ut5/ut7 only when the ut6/ut3 primary is full); `nice`/cgroup caps.

### 7. ut4 recently restored
**Risk:** passed prerequisite checks, but post-restoration availability history is not yet established.
**Mitigation:** run one photometry test and one fit test at low concurrency; confirm logs, outputs, cancellation, and finalizing through the web UI; observe host + NFS stability before promotion.

### 8. Clock synchronization across hosts
**Risk:** worker **lease/heartbeat expiry** (§12) uses wall-clock time; skew between ut2 and workers can cause premature reclaims or confusing errors.
**Mitigation:** verify NTP on all hosts (`timedatectl status`); check drift (`chronyc tracking`); correct drift > 1 s before rollout.

---

## Next steps (tracks MUSCATDB-LITE §12 / port P2 & P9)

1. **Single-host (P2):** run one `muscatdb worker` on ut2 against the SQLite control plane; prove claim / lease / finalize / cancel across the web↔worker boundary.
2. **Multi-host (P9):** install PostgreSQL + `psycopg` (`[cluster]`) on ut2; set `MUSCAT_CONTROL_PLANE=postgres`; run the **unchanged** worker loop on ut6/ut3 as systemd units with core-pinned thread caps; register ut4 on a test queue first; capacity-gate ut5/ut7.
3. **Promote ut4** from test/overflow to regular work after burn-in.

---

## See also
- [MUSCATDB-LITE.md](MUSCATDB-LITE.md) §12 — distributed execution (durable work queue), the authoritative design.
- [CLAUDE.md](../CLAUDE.md) — project standards and environment setup.
- `/etc/hosts` (NIS/IP mapping) and `/etc/exports` (NFS rules) on ut2.
