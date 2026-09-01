# Issue #26 — Staging + production decoupled deployments (execution plan)

Living implementation plan for [issue #26](https://github.com/muscat-team/muscatdb/issues/26)
("Staging server from test, deploy prerequisites"). Tracks the agreed host layout and
the amendments that landed in the issue comment thread. Update this document as the
work proceeds; each phase is host work (as `jerome` on `muscat-ut2`) unless marked
*repo* (a tracked source change).

## Decided layout

From [issue #26 comment](https://github.com/muscat-team/muscatdb/issues/26#issuecomment-5454626239)
(.env/cron here run as `jerome`, so `$HOME` is safe; `deploy.yml` and the shared-input/DB
pins use absolute paths so a future non-`jerome` deploy account cannot mis-resolve them):

| | Production | Staging |
|---|---|---|
| Branch | `main` | `test` |
| Checkout | `$HOME/deploy/main/app` (`/raid_ut2/home/jerome/deploy/main/app`) | `$HOME/deploy/test/app` |
| nginx (public) | `:8000` *(unchanged)* | `:8002` |
| uvicorn (loopback) | `:8001` *(unchanged)* | `:8003` |
| tmux session | `muscatdbgui` *(unchanged)* | `muscatdb-test` |
| `MUSCAT_DB_PATH` | `/raid_ut2/home/jerome/github/research/project/muscat-db/muscat.db` *(unchanged location)* | `$HOME/deploy/test/muscat_test.db` (seeded via `sqlite3 .backup`) |
| `MUSCAT_OBSLOG_DIR` | `/ut2/muscat/obslog` *(unchanged)* | `$HOME/deploy/test/obslog` (own copy, never the shared tree) |
| `MUSCAT_PROSE_DIR` | `$HOME/ql/prose` *(unchanged)* | `$HOME/deploy/test/prose` |
| `MUSCAT_TIMER_DIR` | `$HOME/ql/timer` *(unchanged)* | `$HOME/deploy/test/timer` |
| `MUSCAT_TTV_DIR` | `$HOME/ql/harmonic` *(unchanged)* | `$HOME/deploy/test/harmonic` |
| `MUSCAT_DATA_DIR` | shared, read-only | same (shared, read-only) |
| `MUSCAT_MAX_FULL_JOBS` | `1` *(unchanged)* | `0` |
| `MUSCAT_LCO_MONITOR_ENABLED` | `1` *(unchanged)* | `0` |
| `MUSCAT_LCO_ALLOW_SUBMIT` | set | **unset** — can never book telescope time |
| nginx basic auth | `/etc/nginx/.htpasswd-muscatdb` | **same file** (shared users) |

### Amendments adopted from the comment thread
1. **Drop `--reload`** for both prod and staging in the same change. The CLI already
   defaults `reload=False`; just stop passing the flag in `deploy.yml`.
2. **Pin absolute paths** in `deploy.yml` and **delete the `$HOME`/dev-checkout
   fallback** (`DEPLOY_PATH="${DEPLOY_PATH:-$HOME/github/...}"` is the hazard — it points
   at the dev tree when the var is unset).
3. **Production `.env` names `MUSCAT_DB_PATH` explicitly** so the old
   `github/research/project/muscat-db` tree becomes *only a data location*, never the
   code that runs.
4. **Seed staging with the SQLite backup API, not `cp`**: production is WAL, so
   `cp` of a live `muscat.db` can tear. `sqlite3 "$PROD" ".backup '$STAGING'"` is the
   correct online-backup primitive.

### Amendments adopted during implementation (2026-08-29)
5. **`.env` uses absolute paths, not `$HOME`.** The app loads `.env` with
   python-dotenv, which does not expand `$HOME` or `~` (verified), so
   `MUSCAT_PROSE_DIR=$HOME/ql/prose` would load the literal string. All `.env` paths
   are written absolute (`/ut2/jerome/...`).
6. **Production `.env` also carries the running server's real secrets.** The plan's
   Phase 2 list omitted `LCO_API_TOKEN`, `MUSCAT_DB_SECRET`, `ASTROMETRY_NET_API_KEY`
   and `MUSCAT_LCO_DIR=/data`, which lived only in the `muscatdbgui` tmux shell env.
   A fresh deploy session would lose them, so they are copied into the gitignored prod
   `.env` (values extracted from the running server) to preserve current behaviour.
7. **`MUSCAT_REQUIRE_AUTH=1` set in both `.env` files.** Bare `uvicorn` launch (no
   `--nginx` wrapper) skips `_prepare_nginx_auth()`, leaving `MUSCAT_REQUIRE_AUTH` at
   its default 0. Setting it in `.env` preserves the hardened fail-closed behaviour the
   `restart --nginx` invocation provided today. (The proxy secret is still verified
   either way — it is present in `/etc/muscat-db`.)

### Amendments adopted from PR #119 review (2026-08-31)
8. **Dropped the `strategy.matrix` job for two explicit jobs** (`deploy-production`,
   `deploy-staging`), each with a job-level `if: github.ref_name == '...'`. A matrix
   job's `if` can only see `github`/`needs`/`vars`/`inputs` — `matrix` is not in scope
   there — so a matrix leg cannot skip its own job-level `environment:` claim. Every
   push to either branch was spinning up *both* legs and touching *both* GitHub
   Environments; once either gets required reviewers, an unrelated push opens a pending
   approval on a job that does nothing. Two plain jobs sidestep this since a non-matrix
   job's `if` only needs `github.ref_name`.
9. **Fixed the tmux launch to carry its own `cd`.** The heredoc's `cd "$DEPLOY_PATH"`
   only moves the *SSH shell's* cwd (for `git fetch`/`reset`/`uv sync`); `tmux send-keys`
   targets an *existing* pane with its own unrelated shell and cwd, which the heredoc's
   `cd` never touches. A reused session (`muscatdbgui` already existed) kept launching
   `uv run uvicorn` from wherever that pane last was — the old dev tree — so `uv run`
   resolved the wrong project and `load_dotenv(usecwd=True)` loaded the wrong `.env`,
   silently leaving `MUSCAT_REQUIRE_AUTH` at its fail-open default. Fix: the sent
   keystrokes now do `cd '$DEPLOY_PATH' && uv run ...` themselves, and the `new-session`
   fallback gets an explicit `-c "$DEPLOY_PATH"`.
10. **Reverted the hard-coded absolute paths back to Actions variables**, so each job
    resolves its own value — same value per job as before, but no longer published in a
    public repo.

### Amendments adopted from PR #119 review round 2 (2026-08-31)
11. **`cd ""` does not fail, so the amendment-10 "unset var fails closed" claim was
    wrong.** Verified directly: `cd ""` exits nonzero under bash (`set -e` catches it),
    but exits 0 and leaves cwd unchanged under sh, dash, and zsh. The deploy account's
    login shell is zsh (`/etc/passwd`), and `ssh user@host <<EOF` with no remote command
    runs the login shell against the heredoc — so on the actual target, an unset var
    would silently `git fetch`/`reset --hard` in the SSH session's default directory
    (the account's home) instead of aborting. Fixed with the portable, shell-independent
    guard `: "${VAR:?msg}"` (POSIX parameter expansion, not reliant on `set -e`) right
    after each var is read, verified to abort in bash/sh/dash/zsh alike.
12. **Split the shared `vars.DEPLOY_PATH` / `vars.DEPLOY_TMUX_SESSION` names into
    per-job variables** (`DEPLOY_PATH_PRODUCTION` / `DEPLOY_TMUX_SESSION_PRODUCTION`,
    `DEPLOY_PATH_STAGING` / `DEPLOY_TMUX_SESSION_STAGING`). The identical name in both
    jobs only stayed safe if the values were always set at *Environment* scope, never
    *repository* scope — one repo-level `DEPLOY_PATH` would silently feed both jobs, and
    the staging job would `git reset --hard origin/test` inside the production checkout.
    Distinct names remove that failure mode regardless of which scope they're set at.

### Explicitly out of scope here
- Dedicated service user / systemd (tracked in issue #50, Cloudflare / systemd).
- `systemd vs tmux` decision (deferred; both servers stay under tmux for phase 1).
- Multi-host durable queue worker architecture (issue #51).
- Residual `#81` / `#115` single-CCD data cleanup.
- `#101` `-prod` engine conda envs (orthogonal host work; see `DEPLOYMENT.md` Engine
  checkouts section).

---

## Known host facts (verified 2026-08-29)

- `/ut2` is a symlink to `/raid_ut2/home`; `/ut2/jerome` and `/raid_ut2/home/jerome` are
  the **same inode** (`os.path.samefile` → same). So `$HOME/deploy/...` and
  `/raid_ut2/home/jerome/deploy/...` are physically identical today.
- `$HOME=/ut2/jerome`. No `~/deploy` exists yet — phase 1 creates it.
- Ports 8000/8001 are production's (nginx/uvicorn); 8002/8003 are free.
- `deploy.yml` only deploys on `push: branches: [main]`; secrets are unset, so the
  workflow is currently inert. The fallback at line 47 is what fires destructively the
  moment `DEPLOY_*` secrets are set.
- Nightly cron runs as `jerome` and currently points `MUSCATDB_ROOT` at
  `/ut2/jerome/github/research/project/muscat-db` (the dev tree):
  ```
  MUSCAT_OBSLOG_DIR=/ut2/muscat/obslog
  MUSCATDB_ROOT=/ut2/jerome/github/research/project/muscat-db
  30 17 * * * cd $MUSCATDB_ROOT && bash scripts/download_catalogs.sh >> ... && uv run muscat-db scan-yesterday >> ... && uv run muscat-db build-db >> ...
  ```

---

## Phase 0 — Backups & preconditions (host)

1. Confirm zero running photometry/transit jobs (check `:8001/healthz` + `ps`).
2. Take the daily backup required by `AGENTS.md`, plus a pre-deploy one, and verify it
   opens:
   ```bash
   sqlite3 muscat.db ".backup '$HOME/temp/muscat.db.backup-<ts>-predeploy.sqlite'"
   sqlite3 "$HOME/temp/muscat.db.backup-<ts>-predeploy.sqlite" "PRAGMA integrity_check;"
   ```
3. `ss -ltnp` re-confirm 8002/8003 are free.

## Phase 1 — Create the two checkouts (host)

```bash
mkdir -p "$HOME/deploy/main" "$HOME/deploy/test"
git clone -b main <repo> "$HOME/deploy/main/app"
git clone -b test <repo> "$HOME/deploy/test/app"
uv sync   # in both checkouts. use --dev for prod (it hosts the GUI + cron); staging may be plain
```

Keep `deploy/main/app` tracking `origin/main` and `deploy/test/app` tracking
`origin/test` so `deploy.yml`'s `git fetch origin <branch>` / `git reset --hard
origin/<branch>` target the right ref.

## Phase 2 — `.env` files (host, gitignored)

> Implementation note: paths are written **absolute** (dotenv does not expand `$HOME`);
> `MUSCAT_REQUIRE_AUTH=1` is set in both (bare-uvicorn launch skips `--nginx`); the prod
> `.env` copies the running server's real `LCO_API_TOKEN` / `MUSCAT_DB_SECRET` /
> `ASTROMETRY_NET_API_KEY` / `MUSCAT_LCO_DIR=/data` (amendment 6) so a fresh deploy
> session preserves current behaviour.

- **Production** `$HOME/deploy/main/app/.env`:
  ```
  MUSCAT_DB_PATH=/raid_ut2/home/jerome/github/research/project/muscat-db/muscat.db
  MUSCAT_OBSLOG_DIR=/ut2/muscat/obslog
  MUSCAT_PROSE_DIR=$HOME/ql/prose
  MUSCAT_TIMER_DIR=$HOME/ql/timer
  MUSCAT_TTV_DIR=$HOME/ql/harmonic
  MUSCAT_LCO_MONITOR_ENABLED=1
  MUSCAT_LCO_ALLOW_SUBMIT=1
  MUSCAT_MAX_FULL_JOBS=1
  MUSCAT_OLLAMA_URL=http://127.0.0.1:11434
  ```
  (Copy any other secrets the current dev `.env` carries that production needs.)

- **Staging** `$HOME/deploy/test/app/.env`:
  ```
  MUSCAT_DB_PATH=$HOME/deploy/test/muscat_test.db
  MUSCAT_OBSLOG_DIR=$HOME/deploy/test/obslog
  MUSCAT_PROSE_DIR=$HOME/deploy/test/prose
  MUSCAT_TIMER_DIR=$HOME/deploy/test/timer
  MUSCAT_TTV_DIR=$HOME/deploy/test/harmonic
  MUSCAT_LCO_MONITOR_ENABLED=0
  MUSCAT_MAX_FULL_JOBS=0
  MUSCAT_OLLAMA_URL=http://127.0.0.1:11434
  # MUSCAT_LCO_ALLOW_SUBMIT intentionally left unset
  ```
  `mkdir -p` the staging data dirs (`test/obslog`, `test/prose`, `test/timer`,
  `test/harmonic`).

## Phase 3 — Nightly cron refresh (host) — staging DB seeding + periodic (Q2)

- **Seed once** (never `cp` — WAL safety):
  ```bash
  sqlite3 "$PROD_DB" ".backup '$HOME/deploy/test/muscat_test.db'"
  ```
- **Periodic refresh** is part of the Phase 6 cron rewrite: after prod's
  `build-db`, re-seed staging and re-scan from staging's own `MUSCAT_OBSLOG_DIR` — but
  **no `build-db` on staging**, confirmed destructive at execution (Gate D finding):

  ```bash
  sqlite3 "$PROD_DB" ".backup '$HOME/deploy/test/muscat_test.db'" &&
  cd "$HOME/deploy/test/app" && MUSCAT_OBSLOG_DIR="$HOME/deploy/test/obslog" uv run muscat-db scan-yesterday
  ```
  This keeps `muscat_test.db` a fresh copy of prod every night and exercises the scan
  path against staging's own obslog tree — fully isolated from the shared
  `/ut2/muscat/obslog`. `build-db` is deliberately left out: it drops and rebuilds
  `frames`/`summaries`/`targets` from *only* the CSVs under `MUSCAT_OBSLOG_DIR`, and
  staging's tree starts empty by design, so chaining it after the `.backup` reseed wipes
  the reseed to 0 rows every night. Run `build-db` on staging by hand when actually
  testing that command; don't wire it into the nightly chain.

## Phase 4 — Amend `deploy.yml` (*repo*, PR to `test`) — the code change

- **Two explicit jobs** (`deploy-production`, `deploy-staging`), not a `strategy.matrix`
  — a matrix job's `if` cannot see the `matrix` context, so it cannot gate its own
  job-level `environment:`, and both legs (and both Environments) would run on every
  push. Each job instead carries its own `if: github.ref_name == 'main' | 'test'`.
- **Prod job** (`environment: production`):
  - Drop `--reload` from the launch line (`--port 8001` only).
  - `DEPLOY_PATH`/`DEPLOY_TMUX_SESSION` come from `vars.DEPLOY_PATH_PRODUCTION` /
    `vars.DEPLOY_TMUX_SESSION_PRODUCTION` — a name distinct from staging's, so a
    variable accidentally set at *repository* scope (instead of per-Environment) can
    only ever feed this one job, never cross-contaminate the other checkout. No
    `$HOME`/dev-checkout fallback — an unset/empty var is caught by the portable
    `: "${VAR:?msg}"` guard (verified to abort in bash/sh/dash/zsh; a bare `cd ""` does
    *not* fail under the deploy account's actual shell, zsh) instead of silently landing
    on the dev tree or the SSH session's home directory.
  - The tmux launch command carries its own `cd "$DEPLOY_PATH" &&` (send-keys runs in an
    existing pane whose cwd the script's own `cd` never reaches); the `new-session`
    fallback gets an explicit `-c "$DEPLOY_PATH"`.
- **Staging job** (`environment: staging`): same shape, `vars.DEPLOY_PATH_STAGING` /
  `vars.DEPLOY_TMUX_SESSION_STAGING`, `git fetch/reset origin/test`, uvicorn `--port
  8003`, no `--reload`; gated on the same `configured` secret check (stays inert until
  secrets are set).
- `concurrency: group: deploy-${{ github.ref }}` stays workflow-level and per-ref, so
  prod (`main`) and staging (`test`) runs do not cancel each other.
- Keep `DEPLOY_*` secrets unset (per the issue's ordering — arming deploy must not be
  able to hit the old tree before Phases 1–3 exist).
- CI: `ruff` + fast `pytest` green.

## Phase 5 — nginx staging block (root)

- Add `deploy/nginx-staging.conf`: a second `server { listen 127.0.0.1:8002; ... }`
  proxying to `:8003`, with the **same** `/etc/nginx/.htpasswd-muscatdb`, the same
  `/socket.io` websocket + `X-MuSCAT-Proxy-Secret` include, and the same
  `client_max_body_size 20m`.
- Install to `sites-available`, symlink into `sites-enabled`, `nginx -t`, reload.

## Phase 6 — Move nightly cron to the prod checkout (host, `jerome`)

- Repoint the cron `MUSCATDB_ROOT` from
  `/ut2/jerome/github/research/project/muscat-db` to `$HOME/deploy/main/app`, and add the
  Phase 3 staging-refresh steps. The dev tree then becomes pure dev — free to
  branch-switch with nothing live depending on its working tree.

## Phase 7 — Set Actions vars; verify staging; final gate

- Set `DEPLOY_*` vars (absolute), **no secrets yet**.
- Bootstrap staging once:
  ```bash
  cd "$HOME/deploy/test/app" && uv run muscat-db restart --nginx
  ```
  Verify: `:8003/healthz` → 200; `:8002` (nginx) → 401 (basic auth, same htpasswd);
  `:8001` untouched.
- Smoke-test staging: a `--test_run` photometry/preview run writes only to
  `$HOME/deploy/test/*`; the production DB is unchanged; an LCO submit is refused
  (`MUSCAT_LCO_ALLOW_SUBMIT` unset).
- **Final gate:** only after Phases 1–6 are verified, set the `DEPLOY_SSH_KEY` /
  `DEPLOY_HOST` / `DEPLOY_USER` secrets. Pushing to `main`/`test` then triggers real
  deploys.

---

## Verification checklist
- `nginx -t` passes with both sites enabled.
- `:8001` / `:8003` uvicorn refuse a direct unauthenticated `/` (401); `/healthz` → 200.
- Staging `build-db` never touches the production `muscat.db` (separate file).
- Cron now runs from `$HOME/deploy/main/app` (log paths under the new checkout).
- Repo CI (`ruff`, fast `pytest`) green on the `deploy.yml` change.

## Open items to confirm at execution
- ~~**matrix vs. explicit jobs**~~ → **resolved: two explicit jobs** (deploy.yml PR #119,
  amended after review). A `strategy.matrix` job's `if` cannot see the `matrix` context,
  so it cannot gate its own job-level `environment:` — both legs would run (and both
  Environments get touched) on every push. `deploy-production` and `deploy-staging` each
  carry their own job-level `if: github.ref_name == 'main' | 'test'` instead;
  `concurrency: group: deploy-${{ github.ref }}` stays per-ref so prod/staging runs never
  cancel each other.
- ~~**Staging periodic-refresh chain**~~ → **resolved, the other way**: re-scanning
  staging's own (fully isolated) `MUSCAT_OBSLOG_DIR` and then running `build-db` against
  it is actively destructive, not just a style choice — see the Gate D finding below.
  `build-db` drops `frames`/`summaries`/`targets` and rebuilds only from whatever CSVs
  exist under its `MUSCAT_OBSLOG_DIR`. Staging's tree starts empty by
  design (#71's isolation), so running `build-db` there right after a `.backup` reseed
  wipes the reseed back to 0 rows. Nightly chain drops `build-db` for staging entirely;
  `.backup` (realistic data) + `scan-yesterday` (CSV-only, harmless) is what runs.
- Final `deploy.yml` becomes the source of truth for the exact launch commands; keep
  `notes/DEPLOYMENT.md`'s verification one-liners in sync.

---

## Execution status (2026-08-29)

**Done**
- Phase 0: daily (2026-08-29) + predeploy backups taken, `PRAGMA integrity_check` ok;
  no running photometry/transit jobs; 8002/8003 free.
- Phase 1: `$HOME/deploy/{main,test}/app` cloned (main / test), `uv sync` (prod `--dev`,
  staging plain). Both track their origin branch.
- Phase 2: prod `.env` and staging `.env` written (gitignored), with absolute paths and
  `MUSCAT_REQUIRE_AUTH=1`; verified load in a clean env.
- Phase 3: staging DB seeded from prod via `sqlite3 .backup` (integrity ok).
- Phase 4 (repo): deploy.yml + `deploy/nginx-staging.conf` → **PR #119** (CI green);
  plan doc committed there too. Review round 1 (2026-08-30) found the tmux launch never
  reached the new checkout (amendment 9) and the matrix couldn't gate its own
  `environment:` (amendment 8); both fixed 2026-08-31 along with reverting to Actions
  variables (amendment 10, two explicit jobs instead of a matrix). Review round 2
  (2026-08-31) found the reverted-to variables shared one name across both jobs
  (amendment 12) and that the "unset var fails closed" claim was wrong under the
  deploy account's actual shell (amendment 11); both fixed same day.
- Phase 4b (repo, discovered gap): **PR #120** — `build-db` (and the other `--db` CLI
  commands) now default `--db` from `MUSCAT_DB_PATH`, red-green tested. Required before the
  cron/refresh moves, else `build-db` would write a fresh `./muscat.db` in each checkout.
- Release (2026-08-31): **PR #125** merged `test → main` as a merge commit. This lands
  PR #119 and #120 on `main`, but merging alone does not redeploy anything — see the
  correction below and in Gate B/C.
- Gate C (2026-09-01): `$HOME/deploy/main/app` synced by hand to `origin/main` (`3deda33`)
  and cut over live in tmux `muscatdbgui` — `:8001/healthz` 200, `:8001/` and `:8000/`
  401, `build-db --help` confirms `--db` now defaults from `MUSCAT_DB_PATH` (PR #120 live
  in prod).
- Gate D / Phase 6 (2026-09-01): cron `MUSCATDB_ROOT` repointed to `$HOME/deploy/main/app`;
  staging-refresh steps added (`.backup` reseed + isolated `scan-yesterday`, **no**
  `build-db` — see the finding below and `cronjob.txt`). `logs/` created in both deploy
  checkouts (gitignored, cron's `>>` redirection needs the directory to pre-exist; a
  missing `logs/` would fail closed and abort the rest of the `&&` chain, not silently
  skip). `sqlite3` CLI is not installed on this host — substituted Python's stdlib
  `sqlite3.Connection.backup()`, same backup-API safety against a live WAL DB the plan
  called for, verified standalone (~11s against the live prod DB, no tearing).

**Blocked / not yet done**
- Phase 5 (nginx install): `deploy/nginx-staging.conf` written but install to
  `/etc/nginx/sites-available` + `nginx -t` + reload needs **sudo** (interactive auth
  required on this host). Run as root when convenient.
- Phase 7 (vars + secrets): set `DEPLOY_*` Actions vars (org admin), bootstrap/verify the
  staging server, and only then set the `DEPLOY_*` secrets. This is the final gate.

**Correction (2026-08-31, post #125):** the hand-off checklist below originally had Gate B's
`main` push auto-redeploying production and Gate C verifying that redeploy. It doesn't — the
#125 merge triggered deploy.yml (run `33381068874`), and `Check deploy secrets` reported
`configured=false` (no `DEPLOY_*` secrets/vars exist at repo or environment scope — confirmed
via `gh secret list`, `gh variable list`, and both `production`/`staging` environments), so
every later step was skipped and production kept serving unchanged. Gate F is intentionally
last (see the issue's original sequencing: settle the host layout and verify everything else
before arming secrets), so Gates C and D now do the checkout sync **by hand** instead of
relying on CI. Gate B and C below are corrected accordingly.

**Findings beyond the original plan**
- `MUSCAT_REQUIRE_AUTH` is set to 1 by `restart --nginx` (`_prepare_nginx_auth`); a bare
  `uvicorn` launch skips it, so both `.env` files set `MUSCAT_REQUIRE_AUTH=1` explicitly.
- `load_dotenv` does not expand `$HOME` or `~`, so all `.env` path values are absolute.
- **`build-db` on staging is destructive against an isolated `MUSCAT_OBSLOG_DIR` (found
  2026-09-01, executing Gate D).** Ran the Phase 3 chain by hand before wiring it into
  cron: `.backup` reseed (9,547,144 frames) → staging `scan-yesterday` → staging
  `build-db --db muscat_test.db` left `frames`/`summaries`/`targets` at **0 rows**.
  `build_db()` unconditionally drops and rebuilds those tables from *only* the CSVs under
  `MUSCAT_OBSLOG_DIR`, and staging's tree is empty by design (#71's isolation from the
  shared production tree) — so any `build-db` run there wipes whatever `.backup` just
  seeded. Restored via a second `.backup` (10.4s, 9,547,144 frames back). Nightly chain
  drops `build-db` for staging; keeps `.backup` + `scan-yesterday` only. `build-db` on
  staging is now a manual, deliberate action, not an automatic one.
- `OBSLOG_BASE` (`muscat_db/instruments.py`) is a module-level constant read once via
  `os.environ.get(...)` at import time. A crontab's top-of-file env vars (here
  `MUSCAT_OBSLOG_DIR=/ut2/muscat/obslog`, for prod) are exported to every job process
  spawned from that crontab, and `load_dotenv()` does not override an already-set OS env
  var — so staging's own `.env` value is silently shadowed unless the staging commands
  in the cron line explicitly prefix `MUSCAT_OBSLOG_DIR=...` on the command itself. Without
  that prefix, staging's `scan-yesterday`/`build-db` would resolve `OBSLOG_BASE` to
  production's shared tree, reproducing the #66/#71 incident under the new cron.
- `sqlite3` CLI is not installed on this host (`command -v sqlite3` fails). Substituted
  Python's stdlib `sqlite3.Connection.backup()` (`/usr/bin/python3 -c "..."`, absolute
  path — cron's `PATH` is minimal, same reasoning as the existing `uv` absolute-path
  calls) — same backup-API guarantee against a live WAL-mode DB, no CLI dependency to
  install.

---

## Hand-off checklist — complete Phases 5–7 (2026-08-29)

Execution is blocked on sudo, PR review/merge, and org admin. Work through the gates in
order; verify each gate before moving on.

### Gate A — Merge PRs onto `test` (needs a human reviewer)
1. Review **PR #119** (deploy.yml matrix + `deploy/nginx-staging.conf` + plan doc) →
   merge to `test`.
2. Review **PR #120** (`build-db --db` from `MUSCAT_DB_PATH`, red-green tested) → merge
   to `test`. No explicit `--db` needed in the cron once this lands — the default resolves
   `MUSCAT_DB_PATH` from the checkout's `.env`.

> Self-approval is not possible on `test` (requires a non-author approval). Here the
> issue's body says `Refs #26` — it outlives the merge, so **close by hand** at release.

### Gate B — Release `test → main` — **done** (2026-08-31, PR #125)
3. ~~`gh pr create --base main --head test` (the only PR needing an explicit base).~~ done.
4. ~~Merge with a **merge commit, never squash**~~ (ruleset on `main` enforces this; a squash
   recurses into add/add conflicts on the next test→main merge — see AGENTS.md). Done —
   PR #125 merged as commit `3deda33`.
5. Deploy.yml runs on the `main` push but does **not** redeploy anything: `Check deploy
   secrets` finds no `DEPLOY_*` secrets (Gate F hasn't run yet), so every later step is
   skipped and production keeps serving from wherever it already was. Confirmed on the
   #125 push (run `33381068874`, conclusion `success` with the deploy steps skipped).
   `$HOME/deploy/main/app` is **not** touched by this push — Gate C below does that by hand.

### Gate C — Sync and verify the prod checkout (no sudo needed; done by hand, not via CI)
Gate F is deliberately last (see the issue's original sequencing), so nothing moves the
checkout automatically yet. Mirror by hand what deploy.yml's production job will eventually
do once secrets are set:
6. `cd $HOME/deploy/main/app && git fetch origin main && git reset --hard origin/main && uv sync --dev`.
7. **This is the actual production cutover, not a refresh — do it in a window where you can
   watch `:8001` come back.** Relaunch uvicorn in tmux `muscatdbgui` exactly as deploy.yml
   does (`.github/workflows/deploy.yml:62-69`): stop the old process with a `C-c` sent into
   the pane's own shell, then send the launch command *with its `cd`* into that same
   keystroke — `send-keys` types into the pane's existing shell, which never saw step 6's
   `cd`, so a bare launch command here would relaunch from wherever `muscatdbgui` was last
   sitting (the old dev tree per #26) and load its `.env`, the exact bug this plan exists to
   fix. Skipping the `C-c` instead leaves the old process holding `:8001`, so the new one
   fails to bind.
   ```
   tmux send-keys -t muscatdbgui "" C-c   # then wait a couple of seconds
   tmux send-keys -t muscatdbgui "cd /ut2/jerome/deploy/main/app && uv run uvicorn muscat_db.web:sio_app --host 127.0.0.1 --port 8001" Enter
   ```
8. `:8001/healthz` → 200; full `/` still 401 unauthenticated.
9. From `$HOME/deploy/main/app`: `uv run muscat-db build-db --help` — confirm default
   `--db` resolves to the production path (proves PR #120 is live).

### Gate D — Phase 6: move the nightly cron (host, `crontab -e`) — **done** (2026-09-01)
10. ~~Repoint `MUSCATDB_ROOT=/ut2/jerome/deploy/main/app`~~ done — nightly ingest now runs
    from the prod checkout; no explicit `--db` (`.env` supplies `MUSCAT_DB_PATH`, fix is
    live since Gate C synced the checkout).
11. Add staging-refresh steps after prod ingest — **without `build-db`**, confirmed
    destructive against staging's empty, isolated `MUSCAT_OBSLOG_DIR` (see the finding
    above: it drops and rebuilds from CSVs only, wiping a fresh `.backup` seed to 0 rows).
    The production DB stays at its unchanged location (see the layout table), **not**
    under `$MUSCATDB_ROOT` — a `.backup` against a nonexistent source silently creates an
    empty destination DB (no error), so the wrong source here would leave staging looking
    fine but empty. `sqlite3` CLI isn't installed on this host, so the backup step uses
    Python's stdlib `sqlite3.Connection.backup()` instead (same backup-API guarantee):
    - `/usr/bin/python3 -c "import sqlite3; s=sqlite3.connect('file:/ut2/jerome/github/research/project/muscat-db/muscat.db?mode=ro',uri=True); d=sqlite3.connect('$MUSCAT_TEST_DB'); s.backup(d); d.close(); s.close()"`
    - from `$MUSCAT_TEST_ROOT`: `MUSCAT_OBSLOG_DIR=$MUSCAT_TEST_OBSLOG_DIR scan-yesterday`
      only — the explicit env-var prefix matters here: cron's top-of-file
      `MUSCAT_OBSLOG_DIR` is exported to every job process, and `load_dotenv()` won't
      override an already-set OS var, so staging's own `.env` value would otherwise be
      silently shadowed (see the finding above).
    - `build-db` on staging is intentionally **not** in the nightly chain — run it by hand
      when specifically testing that command.
    - Logs land in each checkout's own `logs/` (created 2026-09-01, gitignored,
      pre-created since cron's `>>` redirection needs the directory to exist first).
    - Full chain recorded in `cronjob.txt`, tracked in git per the repo's existing
      convention for this file.

### Gate E — Phase 5: nginx (needs **sudo**, interactive auth)
12. `sudo cp $HOME/deploy/main/app/deploy/nginx-staging.conf /etc/nginx/sites-available/muscat-staging`
13. Symlink into `sites-enabled`, `sudo nginx -t`, `sudo systemctl reload nginx`.
14. Verify `:8002` reverse-proxies to `:8003` and serves the staging app.

### Gate F — Phase 7: Actions vars + secrets (**org admin**)
15. Set the **vars** `DEPLOY_PATH_PRODUCTION`/`DEPLOY_TMUX_SESSION_PRODUCTION` on the
    `production` Environment and `DEPLOY_PATH_STAGING`/`DEPLOY_TMUX_SESSION_STAGING` on
    `staging` (after Phases 1–6 verified, per issue ordering). `DEPLOY_HOST`,
    `DEPLOY_USER`, `DEPLOY_SSH_KEY` are **secrets**, not vars — set in step 17, not here.
16. Bootstrap staging: `git push` to `test` → staging uvicorn :8003 in tmux
    `muscatdb-test`.
17. Verify :8003 (via :8002) + `/healthz`, then set the `DEPLOY_SSH_KEY`/`DEPLOY_HOST`/
    `DEPLOY_USER` **secrets** for both Environments. From this point on, a push to `main`
    or `test` really does redeploy — Gate B/C's manual sync steps are no longer needed.

### Final verification (from the plan)
- `:8001` / `:8003` refuse unauthenticated `/` (401); `/healthz` → 200.
- Staging `build-db` never touches prod `muscat.db`.
- Prod `:8001` and prod cron both healthy on the new checkout.
- Close issue #26 by hand.

**Live-verification reminders:** after deploy the server runs via bare `uvicorn`
(no `--reload`); HTML/JS changes need a server restart to show. Server runs in tmux
`muscatdbgui` (prod) / `muscatdb-test` (staging).
