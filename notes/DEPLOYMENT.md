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

Keep nginx's `client_max_body_size` (20m in every `deploy/nginx*.conf`) above `MUSCAT_LCO_ARCHIVE_MAX_PAYLOAD_BYTES` (16 MiB by default). Selecting every row of a large LCO archive search posts several MB of frame metadata, and nginx would otherwise refuse it with an HTML 413 before the app can answer in JSON. A whole-request download posts only the request ID.

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

# Restrict an LCO proposal's observations and grant users access (issues #144, #228)
uv run muscat-db access restrict <proposal_id>     # revokes the proposal's `everyone` grant
uv run muscat-db access unrestrict <proposal_id>   # also shares a newly ingested proposal (groups are allow-only)
uv run muscat-db access grant <user> <proposal_id>
uv run muscat-db access revoke <user> <proposal_id>
uv run muscat-db access explain <user> [--target <name>]   # read-only: what <user> sees, per proposal
# Access groups (issue #228): grants are ANDed fields; `user:<name>` is one user
uv run muscat-db access group create <group> [--description "..."]
uv run muscat-db access group add <group> <user>      # / remove
uv run muscat-db access group grant <group> [--proposal P] [--instrument I] [--target T] [--date YYMMDD | --from A --to B] [--note "..."]
uv run muscat-db access group revoke <grant-id>       # kept in group_grants_revoked
uv run muscat-db access group list [<group>]          # read-only
uv run muscat-db access list [--user <user>]
# Scoped viewer (issue #202): sees only the proposals granted to them
uv run muscat-db access scope <user> [--off]
# Scoped viewer + one target under any proposal (issue #228)
uv run muscat-db access grant-target <user> <target>
uv run muscat-db access revoke-target <user> <target>
# Per-user job caps (issue #265): show, set (0 = unlimited) or clear a user's overrides
uv run muscat-db access limit <user> [--running N] [--queued N] [--reset]
```

A restricted proposal is hidden from `/targets`, `/target`, project pages and
the target APIs for every viewer without a grant; admins
(`htpasswd promote <user>`) see everything, and a request with no authenticated
user is treated as having no grants. Restriction only covers frames whose
`proposal_id` is known, so `access restrict` warns about frames that have none.

A *scoped* viewer (`access scope <user>`) inverts the rule: they see only the
proposals granted to them with `access grant`, and nothing else. Frames with no
PROPID (muscat, muscat2, and blank LCO frames) and any proposal not on their
allowlist are hidden, including proposals ingested later. They get no shared
chat (only a private `@bot` exchange and messages addressed to them), and
`/toi` and `/nexsci` do not flag or link targets outside their scope. Admins are
never scoped. A scoped user with no grants sees nothing.

### Per-user job limits

Each user may run `MUSCAT_USER_MAX_RUNNING` (default 1) full jobs at once and
have `MUSCAT_USER_MAX_QUEUED` (default 5) waiting, counted across photometry,
transit fit and TTV fit together. `0` means unlimited. `access limit` stores a
per-user override on the user row; with no override the site default applies,
admins included.

* A launch over the running cap is queued, not refused, and starts when one of
  the user's jobs finishes. Other users' queued jobs are not held up behind it.
* A launch over the queued cap is refused with HTTP 429 and a message naming
  the limit.
* Test runs take no durable slot and are not counted. A job with no user (an
  instance without an authenticating proxy) is not capped.
* Slots claimed before this feature carry no user, so a user's jobs already
  running at deploy time are not counted until they finish.
* The caps are enforced in the job store (`claim_slot`, `enqueue`), not in the
  request handlers, so the web server and fleet workers share them. On
  PostgreSQL each user's claims and enqueues take a transaction advisory lock.

A scoped viewer can also be given single targets with `access grant-target
<user> <target>` (spaces and case ignored, resolved to the raw `object`
spellings): they then see every observation of that target under any proposal,
and nothing else of the proposals it is in. Target grants apply to scoped
viewers only; the command refuses anyone else. `access explain <user>` lists
them. A target first observed under a new spelling after the viewer's grants
were resolved is picked up on the next request.

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

### Release order (test → main)

Merging to `main` deploys nothing by itself; the cron entry for `main` takes the
commit within 5 minutes. Do the steps in this order.

1. **Back up `muscat.db` before the merge**, with SQLite's backup API (not `cp`,
   the file is live and about 2.6 GB). A backup taken after the merge already
   holds the new schema and cannot show the old one.
   ```bash
   python3 -c "import sqlite3; s=sqlite3.connect('file:$MUSCAT_DB_PATH?mode=ro',uri=True); d=sqlite3.connect('$HOME/temp/muscat.db.pre-<release>.<date>'); s.backup(d); d.close(); s.close()"
   ```
2. **Merge the release PR (`test` → `main`) with a merge commit**, never a squash,
   once CI is green on the final `test` head. A later push to `test` dismisses the
   approval.
3. **Wait for the cron deploy** and read `$HOME/deploy/main/app/logs/pull-deploy.log`.
   A migration on the live database can push startup past the health-check limit;
   the first attempt then fails (Slack alert) and the next poll retries. The
   `main` cron line sets `PULL_DEPLOY_HEALTH_TIMEOUT_S=120` for that reason (the
   default is 30).
4. **Verify**: the log says `deployed main at <sha>`, that SHA equals
   `git ls-remote origin refs/heads/main`, `curl 127.0.0.1:8001/healthz` returns
   `{"ok":true}`, and any new tables or columns are in the live database.
5. **Run host-side data steps from `deploy/main/app`**, only after step 4, each as
   `--dry-run` first. They need the deployed code, which is why they follow the
   deploy.

When a PR reads data that an earlier PR only writes or that a command backfills,
ship them in separate releases and run the data step between them. Do not merge the
reader until the data step has run on production:

| Writer (released first) | Data step on the host | Reader (released after) |
|---|---|---|
| Product records written per run (#208, #245) | `muscat-db backfill-products` | Visibility read from `_product.json` |
| Group/grant tables (#228 step 1, #247) | `muscat-db access groups-seed` | Call sites switched from the blocklist to groups (#228 step 2); blocklist retired (#228) |

`backfill-products` first walks the `jobs` table, then the run directories on disk
(`prose/<inst>/<date>/_runs/<target>/<run>` and `timer/<inst>/<date>/<target>[/<run>]`),
because runs older than the `jobs` table have no row. A run with no record is
admin-only for everyone else, so the step is complete only when no run directory is
left without one. Check with a second `--dry-run`: it must report `would write 0
record(s)`. Transit fits found on disk take their light curves from `fit.yaml`; an
unreadable `fit.yaml` or a missing light curve leaves the fit undetermined.

The access groups are the only source of visibility, and they are allow-only:
a proposal ingested after the seed is hidden from every non-admin, and from the
published static site, until an admin runs `access unrestrict <proposal_id>`
(everyone) or `access grant <user> <proposal_id>` (one user). `restrict`,
`unrestrict`, `grant`, `revoke` and `scope` write the groups only; the first of
them on a fresh database seeds the groups. `access list` shows what `everyone`
may not see. The old blocklist tables (`restricted_proposals`,
`user_proposal_access`) are no longer read or written, except by `groups-seed`,
the one-time migration of a database from before the groups; such a database
fails closed for non-admins until it runs. The tables are dropped in a later
release. Do not rerun `groups-seed` to share new proposals: it would grant
`everyone` every new proposal at once, so it refuses on a seeded database
without `--force` (a forced reseed keeps every proposal `everyone` has lost, and
every user grant revoked since the migration).

The files under `/etc/nginx/sites-available/` are root-owned copies, not links to
`deploy/`. A change to `deploy/nginx*.conf` does not reach nginx on a deploy: diff
the live file against the repo, copy it over, then `sudo nginx -t && sudo systemctl
reload nginx`.

### Cloudflare Access tunnel (#50)

External collaborators reach production through Cloudflare Access and an
outbound Cloudflare Tunnel. Nothing listens publicly on ut2:

```
browser → Cloudflare Access (email one-time PIN, allowlist)
        → tunnel (cloudflared, outbound only) → nginx 127.0.0.1:8010
        → auth_request /_auth/cf (JWT checked by the app) → uvicorn :8001
```

`:8010` (`deploy/nginx-access.conf`) has no Basic Auth: the identity is the
email in the Access JWT, which the app validates (RS256, audience, issuer,
expiry) and maps to a `users` row by `users.email`. An email with no row, and an
email that belongs to an admin, get a 403. Admins sign in over `ssh -L` and
htpasswd only; `htpasswd set-email` refuses an admin, and `promote` and
`add --admin` refuse a user who has an email. The `ssh -L` + htpasswd listener on
`:8000` is unchanged.

In the repo: `deploy/cloudflared/muscat-db-tunnel.service` (systemd unit) and
`deploy/cloudflared/tunnel-check.sh` (cron health check). The tunnel is managed
from the Zero Trust dashboard and run from its token, so `cloudflared tunnel
login` never runs on this host and no `cert.pem` tied to a personal account
exists.

**Bring-up**, in order. Steps 1–2 are in the Cloudflare dashboard; the rest on ut2.
Steps 5–8 need #310 (security headers, and `X-Forwarded-Proto https` so chat
connects through the tunnel) and #315 (the admin and unknown-email refusals step 8
checks) released and deployed: step 5 copies `nginx-access.conf` from the deploy
checkout, so it must be the post-#310 file.

1. **Access application.** Zero Trust → Access → Applications → Self-hosted, on
   the public hostname. Login method: one-time PIN only. Policy: Allow, Include →
   Emails, listing each person (never *Everyone* or *Emails ending in*). Copy the
   application's AUD tag; the team domain is `<team>.cloudflareaccess.com`.
2. **Tunnel.** Zero Trust → Networks → Tunnels → Create (cloudflared). Copy the
   token. Public hostname: the same hostname, service `http://127.0.0.1:8010`.
   Under the hostname's Access settings, turn on *Protect with Access* (team name
   and AUD tag) so cloudflared also refuses a request without a valid token.
3. **Install cloudflared** from Cloudflare's apt repository (check
   Cloudflare's current install page for the repository line), so it is patched
   with the host:
   ```bash
   sudo mkdir -p --mode=0755 /usr/share/keyrings
   curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
   echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
   sudo apt-get update && sudo apt-get install cloudflared
   ```
4. **User, token, unit:**
   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin cloudflared
   sudo install -d -m 0700 /etc/cloudflared
   sudo install -m 0600 /dev/null /etc/cloudflared/muscat-db.env
   sudoedit /etc/cloudflared/muscat-db.env        # TUNNEL_TOKEN=<token>
   sudo install -m 0644 deploy/cloudflared/muscat-db-tunnel.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now muscat-db-tunnel
   curl -s 127.0.0.1:20241/ready                  # 200 once connected
   ```
   Until step 5, the public hostname answers 502: nothing listens on `:8010` yet.
5. **Enable the Access listener** (a copy, like the other sites, never a link
   into a checkout):
   ```bash
   cd $HOME/deploy/main/app
   sudo cp deploy/nginx-access.conf /etc/nginx/sites-available/muscat-db-access
   sudo ln -sf /etc/nginx/sites-available/muscat-db-access /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```
6. **Configure the app.** Add to production's `.web-env` (`pull-deploy.sh`
   sources it before every relaunch, so the values survive deploys):
   ```bash
   export MUSCAT_CF_TEAM_DOMAIN=<team>.cloudflareaccess.com
   export MUSCAT_CF_AUD=<AUD tag>
   ```
   Restart `:8001` in its pane, tracing the process to its pane first as
   *Switching an instance over* below describes.
   Until both are set `/_auth/cf` answers 503 and the listener refuses everyone.
7. **Health check cron**, with the public URL so a hostname served without the
   Access login is caught too:
   ```
   */5 * * * * TUNNEL_PUBLIC_URL=https://<hostname>/ bash $MUSCATDB_ROOT/deploy/cloudflared/tunnel-check.sh >> $MUSCATDB_ROOT/logs/tunnel-check.log 2>&1
   ```
   It alerts Slack on a change of state only (down, a different problem,
   recovered).
8. **Verify, team only**, before any collaborator email goes on the allowlist:
   - an unknown email gets the "no MuSCAT-DB account" page; an admin's email gets
     the admin refusal; a mapped non-admin sees what `access explain <user>` says
   - chat connects (Socket.IO) and a running photometry log streams
   - `/var/log/nginx/muscat-db-access.log` shows each request's email and
     `Cf-Connecting-IP`
   - `systemctl stop muscat-db-tunnel` raises the Slack alert within 5 minutes,
     and `start` reports the recovery

**Adding a collaborator.** Not before #310, #312 (re-validated photometry
options, `safe_load`, per-user chat rate limit) and #315 are on `main` and
deployed, and step 8 has passed. Then one at a time:
`muscat-db htpasswd set-email <user> <email>`, the grants (`access group …` or
`access grant-target`), `access explain <user>` to confirm the user sees exactly
what was granted, and only then the email on the Access policy. Remove it from the policy first
when someone leaves. Review the policy against `users.email` every few months.

**Patching.** cloudflared runs with `--no-autoupdate`; it updates with
`sudo apt-get update && sudo apt-get install --only-upgrade cloudflared`, then
`sudo systemctl restart muscat-db-tunnel` and `curl -s 127.0.0.1:20241/ready`.
Do it monthly and on any Cloudflare security advisory for cloudflared; Cloudflare
supports a release for about a year. Record the version in this file when it
changes.

**Rotating the token** (leak, or someone with dashboard access leaves): refresh
the tunnel's token in the dashboard, replace it in
`/etc/cloudflared/muscat-db.env`, restart the unit.

**Rollback** (cuts off every external collaborator, leaves `:8000` alone):
`sudo systemctl disable --now muscat-db-tunnel`, then
`sudo rm /etc/nginx/sites-enabled/muscat-db-access && sudo systemctl reload nginx`.

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
sudo -u postgres createdb -O muscatdb muscatdb_prod   # production control plane (:8000/:8001)
sudo -u postgres createdb -O muscatdb muscatdb_dev    # dev server (:8888) and the dev fleet profile
sudo -u postgres createdb -O muscatdb muscatdb_test   # pytest only, safe to truncate, see warning below
```

Names say the **environment**, not the role, so a database name tells you what it is and what may touch it:

| Database | Used by | Safe to truncate |
|---|---|---|
| `muscatdb_prod` | production web app and the fleet under `--profile prod` | no |
| `muscatdb_dev` | dev server `:8888` and the fleet under `--profile dev` (the default) | yes, but it is the dev job history |
| `muscatdb_test` | `tests/test_job_store_postgres.py` and CI only | yes, the suite truncates it |

The first two names replace `muscatdb_control` and `muscatdb_test`, which were the production and dev databases respectively. On a cluster still carrying the old names, stop every web app and worker first (a rename needs no open connections), then:

```bash
sudo -u postgres psql -c 'ALTER DATABASE muscatdb_control RENAME TO muscatdb_prod'
sudo -u postgres psql -c 'ALTER DATABASE muscatdb_test    RENAME TO muscatdb_dev'   # keeps the dev job history
sudo -u postgres createdb -O muscatdb muscatdb_test                                 # fresh, for pytest
# pg_hba.conf names databases: add a muscatdb_dev line for each worker host (below), reload
```

Keep the DSN out of the repo and out of shell history, in a file only the deploy user can read:

```zsh
mkdir -p ~/.config/muscatdb && umask 077
read -rs "PW?password: " && echo
printf 'export MUSCAT_POSTGRES_DSN=postgresql://muscatdb:%s@localhost:5432/muscatdb_dev\n' "$PW" \
  > ~/.config/muscatdb/pg.env; unset PW
```

(In zsh `read -p` means coprocess; use the `"VAR?prompt"` form. URL-encode `@ : /` in the password.)

### Environment variables

| Variable | Value | Notes |
|---|---|---|
| `MUSCAT_CONTROL_PLANE` | `postgres` (default `sqlite`) | Resolved once on the first `get_job_store()` call, then cached: restart to change it. |
| `MUSCAT_POSTGRES_DSN` | `postgresql://muscatdb:<pw>@localhost:5432/muscatdb_dev` (`muscatdb_prod` for production) | Required when the above is `postgres`. |
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

### Dev server (`:8888`): who runs its full runs

The worker fleet serves one profile at a time (`deploy/workers.py status` prints `installed profile prod but selected dev` when they differ). While the fleet is installed for `prod`, its workers read `muscatdb_prod`, so a job queued on the dev server (`muscatdb_dev`) has no worker to claim it.

A dev server that was started with `MUSCAT_WORKER_MAX_SLOTS=0` (queue-only) therefore leaves every full run `pending`. Seen on 2026-10-16: a full photometry run queued on 2026-10-15 sat `pending` for a day, and a TTV run from the ephemeris page would have done the same.

Do not switch the fleet to `dev` to fix this: `:8001` is queue-only and depends on the same workers. Let the dev server run its own full jobs instead. In the pane that hosts it (trace the process as in the previous section), with nothing queued or running:

```bash
source ~/.config/muscatdb/pg.env && export MUSCAT_CONTROL_PLANE=postgres \
  MUSCAT_WORKER_MAX_SLOTS=2 MUSCAT_MAX_FULL_JOBS=2 \
  && uv run muscat-db restart --port=8888 --reload
```

`MUSCAT_WORKER_MAX_SLOTS=2` lets this process run two full jobs per pipeline itself; `MUSCAT_MAX_FULL_JOBS=2` matches `max_full_jobs` under `[profiles.dev]` in `deploy/workers.toml` (a server launched before that line existed still carries the default 6). Restarting makes the process claim any row still `pending`, so cancel stale queued jobs first or they start at once.

### Warning: the Postgres test suite truncates its tables

`tests/test_job_store_postgres.py` runs `TRUNCATE jobs, job_concurrency_slots` in its fixture. Point it at a database nothing else uses:

```bash
source ~/.config/muscatdb/pg.env
export MUSCAT_POSTGRES_DSN="${MUSCAT_POSTGRES_DSN%/*}/muscatdb_test"
uv run --with 'psycopg[binary,pool]>=3.2' pytest tests/test_job_store_postgres.py -q
```

Do not run it against the database a live instance is using.

### Control-plane backup

`deploy/nightly.sh run` ends with `muscat-db backup-control-plane` when the checkout has a `.web-env` (production's does; it supplies `MUSCAT_POSTGRES_DSN` for `muscatdb_prod`). It runs after `build-db`, so a Postgres outage alerts on Slack without holding up the catalog rebuild. The dump is `pg_dump --format=custom`, read back with `pg_restore --list` before it replaces anything, and written as `<dbname>.nightly-<stamp>.dump` beside the `muscat.db` snapshots, under the same `MUSCAT_DB_BACKUP_DIR` and `MUSCAT_DB_BACKUP_KEEP` (default 2). The password reaches `pg_dump` through `PGPASSWORD`, never its command line. Log: `logs/pg-backup.log`.

Restore, with every worker and the `:8001` web app stopped (they hold connections and would write mid-restore):

```bash
.venv/bin/python deploy/workers.py --profile prod down
source ~/.config/muscatdb/pg.env
pg_restore --clean --if-exists --dbname "${MUSCAT_POSTGRES_DSN%/*}/muscatdb_prod" ~/temp/muscatdb_prod.nightly-<stamp>.dump
```

Jobs that ran after the dump was taken are missing from the restored `jobs` table; their run directories on NFS are untouched.

### Verification record (2026-10-10, ut2)

- Server: PostgreSQL 18.6, role `muscatdb`, schema initialised by `PostgresJobStore` (`jobs`, `job_concurrency_slots`).
- `tests/test_job_store_postgres.py`: 51 passed against `muscatdb_control` (while empty) and `muscatdb_test`.
- Live dev server (`:8888`, `MUSCAT_CONTROL_PLANE=postgres`): a `transit_fit` run (sinistro / 230618 / `tfn-tel14-full_frame-default`) went `running` → `done`, `returncode` 0, 168 s, `attempts` 0, heartbeat refreshed throughout, and `job_concurrency_slots` returned to 0 rows.

### Done

> The entries below were recorded before the databases were renamed, under `muscatdb_test` (now `muscatdb_dev`) and `muscatdb_control` (now `muscatdb_prod`). Until the rename above is run on ut2 the live cluster still has the old names.

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
- [x] Worker fleet declared in `deploy/workers.toml` and run as systemd user units: unit smoke-tested on ut5 (connected, host guard skips it on ut3, `down` stops and disables it).
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

Workers on ut3 and ut5 are declared in `deploy/workers.toml` and run as systemd user units by `deploy/workers.py`. The password stays in `pg.env`, read at start by `deploy/worker-run.sh`; only the host and database are swapped to ut2's:

```toml
[defaults]
pg_host   = "157.82.46.83"      # ut2
database  = "muscatdb_dev"      # muscatdb_prod via [profiles.prod]
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
.venv/bin/python deploy/workers.py down ut5       # refuses while jobs run there or under another profile; --force to override
eval "$(.venv/bin/python deploy/workers.py web-env)"   # in the shell that starts the web app
```

Dev and production share this one fleet and one set of unit names; they rarely run together, so `[profiles.dev]` and `[profiles.prod]` in `workers.toml` switch the keys that must change as a pair. `prod` sets `database = "muscatdb_prod"` **and** `repo = /ut2/jerome/deploy/main/app`, because the workers run the code in `repo`: a production database with the dev checkout would run dev code on production jobs. `--profile NAME` (or `MUSCAT_WORKERS_PROFILE`) selects one; `[defaults] profile` is used otherwise, and every command prints the active profile, database and repo. To switch:

```bash
.venv/bin/python deploy/workers.py --profile dev  down    # refuses while jobs run on a host; up skips busy workers likewise
.venv/bin/python deploy/workers.py --profile prod up      # env text changed, so the units restart
eval "$(.venv/bin/python deploy/workers.py --profile prod web-env)"   # in the shell that starts :8001
```

Before the first `prod` switch: allow `muscatdb_prod` for each worker host in `pg_hba.conf` (the old `muscatdb_control` is refused today), and decide `max_full_jobs` (the prod profile sets hosts + 1, currently `6`; the dev profile sets `2`). While `prod` is active, the dev server on `:8888` has no worker claiming from `muscatdb_dev`, so run it with `queue_only = false` or switch back.

`pull-deploy.sh` runs `uv sync --dev --extra cluster`, so `psycopg` survives each deploy (the prod workers run the deploy checkout's interpreter). It also sources `$DEPLOY_PATH/.web-env` (override: `PULL_DEPLOY_WEB_ENV_FILE`) before `muscat-db serve --nginx` when that file exists, so a respawned `:8001` keeps its queue-only and control-plane variables. The file is not committed and absent by default, which changes nothing; to put `:8001` on the Postgres control plane, create it from `workers.py --profile prod web-env` plus the `MUSCAT_CONTROL_PLANE=postgres` and DSN exports. That switch does not migrate job history, so decide it deliberately.

Run it from ut2 (it needs Python >= 3.11; the workers' system python is 3.10). Once per host, `loginctl enable-linger $USER` so the units survive the last logout; `up` warns when it is off.

Adding another host takes two steps on ut2 that need `sudo` in a real terminal, then one command:

```bash
sudo cp -p /etc/postgresql/18/main/pg_hba.conf /etc/postgresql/18/main/pg_hba.conf.bak-$(date +%Y%m%d)
echo "host  muscatdb_dev  muscatdb  <host-ip>/32  scram-sha-256" | sudo tee -a /etc/postgresql/18/main/pg_hba.conf
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
- [x] Fixed (#51 step B): `_ensure_pg_jobs_schema` takes a transaction-scoped advisory lock first, so concurrent sessions queue instead of deadlocking. Cause: `CREATE INDEX IF NOT EXISTS` takes a SHARE lock on `jobs` and the `ALTER TABLE` after it needs ACCESS EXCLUSIVE, so two sessions each held the lock the other was upgrading past. `tests/test_job_store_postgres.py::TestSchemaEnsureConcurrency` reproduces it (six sessions, a missing column each round) and fails with `DeadlockDetected` without the lock. `stagger_s` is no longer needed for this, but is harmless.

### Not covered yet

- [ ] Orphan reclaim (#174), retry limit: kill the same job repeatedly and confirm it errors after `MUSCAT_JOB_MAX_RECONCILE_ATTEMPTS` (a single kill and requeue was verified, see above).
- [ ] `MUSCAT_JOB_NOTIFY=1` wake-up latency over real Postgres `LISTEN`/`NOTIFY` (the flag was on for the ut3 worker, but latency was not measured).
- [ ] #209 access gating against a Postgres-backed job list.
- [ ] A job actually claimed and run **on ut5** (its three workers are connected and idle; every job so far ran on ut3 or ut2).
- [ ] Network exposure beyond ut3 and ut5: `pg_hba.conf` and `ufw` entries for ut4, ut6 and ut7, and a least-privilege worker role (the current `muscatdb` role owns both databases).
- [ ] `psycopg` in the worker environments of ut4, ut6 and ut7 (ut3 and ut5 use the shared repo venv).
- [ ] Move the running tmux workers on ut3 and ut5 to the systemd units (`workers.py down`, then `up`), and `loginctl enable-linger` on both (currently `Linger=no`, so units would stop at the last logout). The unit was smoke-tested on ut5 with one extra `ttv_fit` worker; the existing workers were not migrated.
- [ ] Merge muscat-team/muscatdb#218 and restart the dev server: until then a full run queued after a test run on the same key can be overwritten, and `/jobs` re-run of an unnamed run forks a row.
- [ ] Reboot test of the units (needs linger).
- [x] Fix the concurrent schema-check deadlock (see the section above).
- [ ] Real host routing: today it is the `MUSCAT_WORKER_MAX_SLOTS=0` workaround, not a queue that targets a host.
- [ ] Cancelling a job that runs on another host.
- [x] Backups: the control plane is dumped nightly (#51 step C, see *Control-plane backup* below).
- [ ] Decide whether SQLite job history should be migrated or left behind.
- [ ] Switch production (`:8001`) and the test deploy (`:8003`) only after the above and a release.

---

## Database integrity failure

The nightly `build-db` copies `muscat.db` with the SQLite backup API, runs
`PRAGMA integrity_check` on the copy, and refuses to rebuild over a file that
fails (#182). A refusal exits non-zero, so `nightly.sh` posts a Slack alert with
the step name and the end of `logs/build-db.log`.

### Read the reason first

`logs/build-db.log` names the first problems and the kept copy:

- **Only `Page N: never used` lines** are unreferenced pages. Usually they are
  leaked accounting, but a subtree that lost its parent pointer is reported the
  same way and its rows are gone. The rebuild regenerates `frames`, `summaries`
  and `targets` only; the app-owned tables (users, access, groups, jobs, LCO
  requests) are copied forward as they are. So the build carries on, Slack gets
  an alert, the snapshot is marked (`<snapshot>.leaked-pages`), and the newest
  clean snapshot is kept however many leaked ones follow it. A leaked copy with
  fewer rows in any app-owned table than that clean snapshot is refused as
  damage. After an alert, compare the app tables against the clean snapshot
  and restore from it if rows are missing.
- **Anything else** is damage. The copy is kept as
  `muscat.db.nightly-<stamp>.sqlite.INTEGRITY-FAILED` in `$MUSCAT_DB_BACKUP_DIR`
  (default `~/temp`). Nothing was changed, but no rebuild will run until the file
  is sound.

### A slow run is not a hung run

The copy and the check each read the whole file, and the log records both times
(`copy N s, check N s`) and warns when they total more than 30 minutes. On
2026-10-20 they took 3 h 11 min, because the controller's weekly consistency
check and patrol read (scheduled for 12:00; `storcli64 /c0 show cc`, `show pr`)
were running on the same array. The `.part` file stays at 0 bytes until the copy
commits. Do not kill the job: the verdict arrives at the end.

### Restore from a verified snapshot

1. Find the newest `muscat.db.nightly-*.sqlite` that passes, checking it
   read-only (about 90 minutes on a busy disk):
   `python3 -c "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?immutable=1',uri=True); print(c.execute('PRAGMA integrity_check').fetchall())" <snapshot>`
2. List what the damaged file holds that the snapshot lacks. `frames`, `summaries`
   and `targets` are rebuilt by `build-db`; compare the app-owned tables (jobs,
   chat, LCO requests, users, notes) row by row, read-only, and save the
   difference. A table the damaged file cannot read is lost.
3. Stop `nightly.sh` and the servers (they run in tmux panes; save each server's
   command, working directory and environment from `/proc/<pid>/` first so it
   can be restarted as it was). Confirm nothing holds `muscat.db` open.
4. Move `muscat.db`, `-wal` and `-shm` to `muscat.db.CORRUPTED-<date>*`. Never
   delete them.
5. Copy the verified snapshot to `muscat.db`, `chmod 644`, and `cmp` it against the
   snapshot (only SQLite header bytes 19, 20, 28 and 96 may differ once a
   connection has switched it to WAL).
6. Start the servers, insert the saved rows (`INSERT OR IGNORE`), run `muscat-db
   build-db`, then `muscat-db backfill-products --dry-run`, which must report 0
   records if the product records are intact.

### Cause

Unknown. Corruption was found on 2026-09-11, 2026-10-11 (leaked pages, repaired
with `VACUUM INTO`) and 2026-10-19 (`lco_monitor_leases` unreadable, restored
from the 2026-10-19 00:30 snapshot). The RAID 6 array was `Optimal` on
2026-10-20, its CacheVault healthy, with no drive, media or power events logged
since boot, so a hardware fault is not indicated. Look for what interrupts a
write: a killed process, a long-running writer during a backup, or several
servers on one file.

The likeliest cause is NFS (#298). Workers on ut3–ut7 opened the catalog over
the `/mnt_ut2` mount, which SQLite does not support: its POSIX byte-range locks
and WAL shared memory only work between processes on one host. On 2026-10-23
page 31 (`idx_lco_observation_due`) was zeroed overnight. That afternoon, and
again overnight into 2026-10-24, ut6's NFS client lost its state while holding a
SQLite lock on `muscat.db`. Each time nfsd kept the lock on ut6's behalf, and
every writer on ut2 failed with `database is locked`, for 2 h 15 m the first time
and 8 h 16 m the second. Both ended when ut2 expired ut6's client state:

```
sudo sh -c 'for c in /proc/fs/nfsd/clients/*; do grep -q "<client IP>" $c/info && echo expire > $c/ctl; done'
```

Since #298 a worker on the Postgres control plane cannot open the catalog
(`database.forbid_catalog`, set by `worker.run`). The web process on ut2
refreshes target status for jobs workers finish, and a queued row carries the
frame inputs and running cap the drain needs. A worker job that finishes while
the web process is down, or within one reconciliation pass of being queued,
keeps its old Targets-page status until the nightly `build-db`. A row queued
before #298 deployed launches without a product record; run `muscat-db
backfill-products` to write it.

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
#26): it runs `deploy/nightly.sh run` (catalogs, prod `scan-yesterday`,
`build-db`, control-plane backup), then reseeds `muscat_test.db` from prod via
SQLite's backup API and runs an isolated staging `scan-yesterday` against
staging's own `MUSCAT_OBSLOG_DIR`. A failed catalog download does not stop that
chain: `nightly.sh run` posts it as a warning and still exits 0. The README's
[Cron section](../README.md#cron-daily) gives the generic, host-agnostic form
for anyone else deploying this repo; the literal nightly entries installed on
this host, staging refresh included, are (`crontab -l`, 2026-10-25):

```
MUSCAT_OBSLOG_DIR=/ut2/muscat/obslog
MUSCATDB_ROOT=/ut2/jerome/deploy/main/app
MUSCAT_TEST_ROOT=/ut2/jerome/deploy/test/app
MUSCAT_TEST_DB=/ut2/jerome/deploy/test/muscat_test.db
MUSCAT_TEST_OBSLOG_DIR=/ut2/jerome/deploy/test/obslog
30 17 * * * cd $MUSCATDB_ROOT && UV_BIN=/ut2/jerome/.local/bin/uv bash deploy/nightly.sh run && /usr/bin/python3 -c "import sqlite3; s=sqlite3.connect('file:/ut2/jerome/github/research/project/muscat-db/muscat.db?mode=ro',uri=True); d=sqlite3.connect('$MUSCAT_TEST_DB'); s.backup(d); d.close(); s.close()" >> $MUSCAT_TEST_ROOT/logs/staging-refresh.log 2>&1 && cd $MUSCAT_TEST_ROOT && MUSCAT_OBSLOG_DIR=$MUSCAT_TEST_OBSLOG_DIR /ut2/jerome/.local/bin/uv run muscat-db scan-yesterday >> $MUSCAT_TEST_ROOT/logs/scan.log 2>&1
30 21 * * * cd $MUSCATDB_ROOT && UV_BIN=/ut2/jerome/.local/bin/uv MAX_BUILD_AGE_HOURS=24 bash deploy/nightly.sh check
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

Record, 2026-10-13: prose2 `github.com/jpdeleon/prose2` @ `main` `eb8e8183`
(clean). It includes jpdeleon/prose2#11 (`ep60` SOPHIA camera name; `up`/`Y`/`w`
sinistro saturation limits) and #12 (opt-in `keyword_gain`/`keyword_read_noise`
header override). Observed on ut2, ut3, ut4 and ut5, which share one checkout
through `/ut2`, so there is one copy to keep current.

Camera definitions come from `~/.prose/*.telescope`, which are **symlinks into
`prose2/data/`**, so a `git pull` of the checkout is the whole refresh.
`prose.CONFIG.update_builtins()` is not part of it: it rewrites only the ten
observatory files in `prose/builtins.py` and never touches `data/*.telescope`. A
`.telescope` file added upstream needs one `ln -s` into `~/.prose` (done for
`qhy600` and `sbig` on 2026-10-13).

A bare `FITSImage(path)` resolves its telescope by matching the header `INSTRUME`,
then `TELESCOP`, against each file's `names`. As of this record only Sinistro
(`faXX`, `ep60`) matches real LCO frames; MuSCAT3/4, SBIG and QHY600 frames fall
back to the default telescope (gain 1, read noise 9), so their `keyword_*` opt-ins
are not reached yet.

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

#### Pinned production engines

`deploy/engines.toml` records each engine's repository, full commit, and
revision-specific production environment name. The repository must be the
owner's; `engines.py` rejects a fork. `deploy/engines.py` audits the development baseline,
clones its working dependencies into a **new** environment, replaces the editable
engine with the pinned non-editable Git install, and verifies repository, commit,
interpreter, import location, and `pip check`. It refuses to overwrite an existing
environment. A failed provision leaves an unapproved environment for diagnosis;
do not select it for production. An existing environment must pass `verify` before use.

The current pins are:

| Engine | Repository | Commit | Candidate environment |
|---|---|---|---|
| prose2 | `jpdeleon/prose2` | `3daed56b8b4bf23b6cf3a19f8f17d898b8c15785` (v3.4.0) | `prose-prod-3daed56b` |
| timer | `john-livingston/timer` | `b8428258c9b62266532173af4abd7fbafee6dcef` | `timer-prod-b8428258` |
| harmonic | `john-livingston/harmonic` | `a32fb8e32ca776e0e6e7dc92ad4232cef1f12194` | `harmonic-prod-a32fb8e3` |

The 2026-10-18 audit below covered prose2 `6029ea87` and timer. prose2
v3.4.0 (`3daed56b`) adds jpdeleon/prose2#16 (read-only configuration loading) and
leaves telescope definitions unchanged; `harmonic-prod-a32fb8e3` is new. Both were
installed and verified on 2026-10-24 (see the activation record below).

These retain the working baseline's Python (prose 3.10.15, timer 3.13.13,
harmonic 3.12.13) and
science dependencies. Prose's one explicit dependency override is
`matplotlib==3.8.4`: the working environment's 3.9.3 violates the current
owner package's `matplotlib<3.9` constraint (the old editable metadata did not
report this). The override is recorded in `engines.toml` and verified against the
installed version. On success,
`<environment>/muscat-engine.json` records the installation identity, exact conda
package URLs, and full pip freeze. Preserve this record with each deployment.
Cloning is a migration from an audited working baseline, not a portable lockfile
installer: a later clone of a changed development environment may have different
dependencies even at the same engine commit. Compare dependency records before
accepting any rebuilt environment.

**Activated on 2026-10-24** for the production web app (`:8001`) and the fleet
(ut3–ut7), with `main` at `5220fef1`:

- `deploy/engines.py install prose harmonic` provisioned `prose-prod-3daed56b` and
  `harmonic-prod-a32fb8e3` on ut2; `timer-prod-b8428258` was reused. `verify` passes
  for all three on ut2 and ut3–ut7, which share `/ut2/jerome/miniconda3`.
- The dependency records are checked in under
  `deploy/conda/production/{prose,timer,harmonic}.json`.
- `tests/test_pinned_engines_slow.py` passed for photometry preview and full
  reduction (the pinned run launched frozen, through the same job environment and
  log header as production) and for timer optimize-only and seeded sampling. The
  real Sinistro/260624 TIC88297141 light curve is identical, and timer's real KIC
  multi-band smoke fit has identical posterior means. The sampling check uses only
  20 tuning and 20 draws; it tests migration compatibility, not scientific
  convergence.
- harmonic has no slow comparison yet. A replay of the recorded TOI7504 two-planet
  fit (`-l bc`, seed 42) against the dev env gave the same ΔBIC (−12.40), χ² equal to
  about 1e-7, and every posterior median within 0.064 posterior SD; the remaining
  difference is the owner's parameter order. A single planet letter without
  `--non-transiting-outer` has no planet pair and so no TTV model; the owner's
  commit fails it with `KeyError: 't0_<letter>'`.
- `deploy/workers.py --profile prod up` rendered the engine selections into the
  worker env files, and the four engine lines from `web-env` were added to
  production's `.web-env` before the web process was restarted in `muscatdbgui`.
  Pinned engines now report `version+git.<SHA>`.

The pre-activation `.web-env` and worker env files are kept in
`~/temp/rollout-20261024/` for the Rollback below; the dev and earlier `-prod`
environments are untouched.

**Production photometry uses a frozen effective telescope configuration.**
With `MUSCAT_PROSE_FROZEN_CONFIG=1`, reductions and postprocessing run
`<pinned env>/bin/python -I -m <module>` with `PROSE_CONFIG_FILE` pointing at the
packaged `_prose_telescopes.yaml` and `PROSE_CONFIG_SHA256` set to its digest.
prose2 (jpdeleon/prose2#16) then loads that file read-only before touching HOME:
it never reads `~/.prose` or development telescope symlinks, never creates shared
config files, never downloads the unused Ballet model, and rejects configuration
writes. A missing or changed file fails without falling back to `~/.prose`.
prose exports both variables, so spawn and forkserver workers load the same file;
the adapter's fork-only restriction is gone. This is one authoritative,
version-controlled configuration, not a configuration directory per environment.
muscat-db checks the packaged checksum before each launch. `-I` keeps cwd,
`PYTHONPATH` and user site-packages from shadowing the pinned install.

Frozen mode refuses to launch unless the selected env reports a pinned,
non-editable prose (`version+git.<SHA>`), and refuses `MUSCAT_PROSE_PYTHON`,
which would bypass the selected env.

The snapshot preserves all 94 effective aliases, including builtin/custom
definitions, resolved duplicate-name precedence, and YAML tuple types. It records
the source telescope-file hashes. No camera values or header precedence are
changed by freezing them. It was captured at prose2 `6029ea87`; its
`engine_commit` field records that capture, not the current pin. The digest is
written at the top of each run log and stored as
`tool_versions.prose_config_sha256` in `_product.json`. `engines.py verify`
imports the real pinned engine with the file selected the way production launches
it, and requires a read-only configuration with the pinned digest; an engine
without #16 fails this check. The YAML is included in the installed muscat-db wheel.

To prepare a future configuration update, `deploy/engines.py capture-config
--snapshot <new-file>` captures the effective configuration using clean engine
code at the approved commit. It refuses to overwrite an existing snapshot. Review
the candidate's scientific settings, then update the packaged YAML and both
recorded SHA-256 values (`muscat_db/prose_config.py` and `engines.toml`) together.
Never regenerate the production snapshot automatically at launch or by following
live symlinks.

**Harmonic runs on the owner's upstream with its default Lithwick priors.**
`john-livingston/harmonic` at `a32fb8e32ca776e0e6e7dc92ad4232cef1f12194` has no
`--mu-min-me`, `--mu-max-me` or `--z-max` CLI flags, so muscat-db no longer
offers or sends them (#77). The constraints step uses harmonic's defaults:
log-uniform mass 1–30 M⊕ per M☉ and |Z_free| ~ U(0, 0.1). Those bounds only
affect the post-fit Lithwick mass/eccentricity constraints in `harmonic.log`,
not the MCMC fit, samples, ΔBIC or plots. All earlier runs used exactly these
values. Adding the flags upstream is proposed in john-livingston/harmonic#1;
re-add the GUI inputs only once a pinned upstream commit has them.

**A selected engine env never falls back.** The production profile selects each
engine's env through `MUSCAT_<ENGINE>_CONDA_ENV`. When that env is missing, all
three launchers raise instead of falling back to `conda run`, an executable on
`PATH`, or the prose2 checkout. Development, which leaves these unset, keeps the
fallbacks. A checkout whose `.env` names an env that does not exist on that host
therefore stops launching that engine: provision the env or unset the variable
before deploying this change.

Prepare without changing any live process:

```bash
uv run python deploy/engines.py audit
uv run python deploy/engines.py install
uv run python deploy/engines.py verify
```

Use `--conda /absolute/path/to/miniconda3/bin/conda` for another installation.
Temporary build files go under `~/temp` (`--scratch` overrides this).
Verify on **each** worker host as well as ut2, and exercise the installed
`photometry`, `timer-fit` and `harmonic` entrypoints against isolated output directories.
Check telescope resources and both test/full execution paths. Run the fast suite
with `uv run pytest`; heavyweight integration checks must use separate databases,
output trees and obslog paths, never a default production run directory.

Roll out only after the PR is merged, released and taken by the deploy host:

1. Take a verified SQLite backup; record the current web and worker engine settings.
2. Confirm `deploy/engines.py verify` succeeds everywhere and that no jobs are active.
3. `deploy/workers.toml [profiles.prod].engine_envs` selects pinned prose, timer and
   harmonic for **both** `render_env` and `web-env`; `prose_frozen_config = true`
   enables the frozen configuration in both processes. Development keeps its existing environments
   and exports `MUSCAT_PROSE_FROZEN_CONFIG=0`.
   Apply with `uv run python deploy/workers.py --profile prod up`. Busy workers are
   deliberately left unchanged; re-run after they drain and check all units.
4. Merge the `web-env` engine exports into production's existing `.web-env`, preserving
   its Postgres and control-plane settings. Do not replace that file with only the
   generated fragment. Ensure `MUSCAT_PROSE_PYTHON` is unset in both `.env` and the
   launch environment: frozen mode refuses to launch while it is set.
5. Restart the production web process in `muscatdbgui`, verify `/healthz`, then run
   one queued job per migrated pipeline and check its log and `_product.json`.
   Pinned engines now record `version+git.<full SHA>` in the existing version fields.
   Editable (dev checkout) engines record `version+git.<HEAD SHA>.editable`, with
   `.dirty` appended when tracked files differ from HEAD; a `.editable` version in a
   production `_product.json` means the pinned env was not selected.

Rollback: stop admitting new work and let active jobs finish, restore the recorded
web exports and worker `engine_envs` (an operator-local config can select the old
envs), reapply the fleet configuration, and restart the web process in `muscatdbgui`.
When reverting to editable prose, also set `prose_frozen_config = false` for the
workers and `MUSCAT_PROSE_FROZEN_CONFIG=0` for the web process; the frozen launcher
deliberately refuses editable engines.
No old engine environment is removed by this workflow. Future upgrades provision
new environment names and repeat this process; never upgrade an in-use env in place.

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
