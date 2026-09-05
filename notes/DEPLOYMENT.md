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

`deploy/pull-deploy.sh` deploys each branch to its own checkout, intended to run from a cron entry per checkout (branch, tmux session and port are its three arguments). Those entries are not installed yet (#26 Gate F), so deploys are currently a manual `git reset --hard` plus relaunch in the checkout. The script polls `git ls-remote` for a new SHA, resets to it, relaunches, then verifies `/healthz` before recording the deploy as good. The earlier `deploy.yml` push deploy was deleted in #133: GitHub-hosted runners cannot reach this host without the VPN, so it never worked, and the `DEPLOY_PATH_*`/`DEPLOY_TMUX_SESSION_*` actions variables it would have used are unused. Each checkout's `.env` pins the absolute paths above. Both launch uvicorn **without `--reload`** (dropped as part of the #26 host split — see the "Authentication boundary" note below for why `--reload` was harmful). `deploy/setup-nginx.sh` handles first-time nginx setup for production; staging's block lives at `/etc/nginx/sites-available/muscat-db-staging` (`:8002` → `:8003`).

The README "Multi-User Deployment" section has the full walkthrough; the essentials:

```bash
# First-time nginx setup (as root)
sudo bash deploy/setup-nginx.sh

# Manage users (writes the htpasswd file + the SQLite users row)
sudo env "PATH=$PATH" uv run muscat-db htpasswd add <user>
uv run muscat-db htpasswd delete <user>
uv run muscat-db htpasswd list
```

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
- The cluster probe found **no PostgreSQL server or `psycopg`** on any host. These are the `[cluster]` prerequisite for the multi-host control plane (§12) and must be installed/tested before rollout. **Redis and Celery are not used** by the §12 design.

### Present on ut2 only
- **`uv` package manager** — used by the web app (`uv run`).
- On workers, either install `uv` per host or run worker entrypoints via the shared conda environments.

### Required before multi-host rollout
- Install PostgreSQL on ut2 and `psycopg` in the selected worker environment(s) (`muscatdb[cluster]`).
- Verify clock sync (NTP) across all participating hosts (lease/heartbeat expiry depends on it).

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
