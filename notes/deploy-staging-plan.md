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
  `build-db`, re-seed staging and re-ingest from staging's own `MUSCAT_OBSLOG_DIR`

  ```bash
  sqlite3 "$PROD_DB" ".backup '$HOME/deploy/test/muscat_test.db'" &&
  cd "$HOME/deploy/test/app" && uv run muscat-db scan-yesterday && uv run muscat-db build-db
  ```
  This keeps `muscat_test.db` a fresh copy of prod, then ingests from staging's own
  obslog tree — fully isolated from the shared `/ut2/muscat/obslog`. (Confirm the exact
  chain at execution; see open item below.)

## Phase 4 — Amend `deploy.yml` (*repo*, PR to `test`) — the code change

- **Prod job:**
  - Drop `--reload` from both launch lines (`--port 8001` only).
  - Replace the `DEPLOY_PATH`/`DEPLOY_TMUX_SESSION` fallback lines with the hard-coded
    `/raid_ut2/home/jerome/deploy/main/app` and `muscatdbgui` (no default-variable
    indirection, no `$HOME` fallback to the dev tree).
- **New `staging` job:** triggers on `push: branches: [test]`, deploys to
  `/raid_ut2/home/jerome/deploy/test/app`, tmux `muscatdb-test`, uvicorn `--port 8003`,
  no `--reload`; gated on the same `configured` secret check (stays inert until secrets
  are set).
- Prefer a **matrix** over two copy-pasted heredocs (prod vs staging differ only in
  branch / checkout / tmux / port); validate that `concurrency: group:
  deploy-${{ github.ref }}` stays per-ref so prod (`main`) and staging (`test`) runs do
  not cancel each other.
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
- ~~**matrix vs. explicit jobs**~~ → **resolved: matrix** (deploy.yml PR #119). The two
  jobs differ only in branch/checkout/tmux/port, so a single templated body via a
  `strategy.matrix` is used; `concurrency: group: deploy-${{ github.ref }}` stays per-ref
  so prod/staging runs never cancel each other. Each matrix entry is gated on its branch.
- **Staging periodic-refresh chain** — whether staging `build-db` reuses prod obslog
  CSVs or re-scans staging's own `MUSCAT_OBSLOG_DIR`. Default: re-scan staging's own
  (fully isolated), seeded from the prod DB snapshot.
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
- Phase 4 (repo): deploy.yml matrix + `deploy/nginx-staging.conf` → **PR #119** (CI green);
  plan doc committed there too.
- Phase 4b (repo, discovered gap): **PR #120** — `build-db` (and the other `--db` CLI
  commands) now default `--db` from `MUSCAT_DB_PATH`, red-green tested. Required before the
  cron/refresh moves, else `build-db` would write a fresh `./muscat.db` in each checkout.

**Blocked / not yet done**
- Phase 5 (nginx install): `deploy/nginx-staging.conf` written but install to
  `/etc/nginx/sites-available` + `nginx -t` + reload needs **sudo** (interactive auth
  required on this host). Run as root when convenient.
- Phase 6 (cron move): must wait for **PR #120** to reach the production checkout
  (`test` merge + `test`→`main` release + redeploy), otherwise `build-db` targets the
  wrong file. Then repoint `MUSCATDB_ROOT` to `$HOME/deploy/main/app` and add the Phase 3
  staging-refresh steps.
- Phase 7 (vars + secrets): set `DEPLOY_*` Actions vars (org admin), bootstrap/verify the
  staging server, and only then set the `DEPLOY_*` secrets. This is the final gate.

**Findings beyond the original plan**
- `MUSCAT_REQUIRE_AUTH` is set to 1 by `restart --nginx` (`_prepare_nginx_auth`); a bare
  `uvicorn` launch skips it, so both `.env` files set `MUSCAT_REQUIRE_AUTH=1` explicitly.
- `load_dotenv` does not expand `$HOME` or `~`, so all `.env` path values are absolute.

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

### Gate B — Release `test → main`
3. `gh pr create --base main --head test` (the only PR needing an explicit base).
4. Merge with a **merge commit, never squash** (ruleset on `main` enforces this; a squash
   recurses into add/add conflicts on the next test→main merge — see AGENTS.md).
5. Deploy.yml auto-runs on the `main` push (pins prod checkout with
   `git reset --hard origin/main`). **Prod uvicorn relaunches on :8001 in tmux
   `muscatdbgui`; prod now has the `--db` fix.**

### Gate C — Verify prod redeploy (no sudo needed)
6. `:8001/healthz` → 200; full `/` still 401 unauthenticated.
7. From `$HOME/deploy/main/app`: `uv run muscat-db build-db --help` — confirm default
   `--db` resolves to the production path (proves PR #120 is live).

### Gate D — Phase 6: move the nightly cron (host, `crontab -e`)
8. Repoint `MUSCATDB_ROOT=/ut2/jerome/deploy/main/app` so nightly ingest runs from the
   prod checkout; no explicit `--db` (fix is live).
9. Add staging-refresh steps after prod ingest:
   - `sqlite3 "$MUSCATDB_ROOT/muscat.db" ".backup '/ut2/jerome/deploy/test/muscat_test.db'"`
   - from `$HOME/deploy/test/app`: `scan-yesterday` +
     `build-db --db /ut2/jerome/deploy/test/muscat_test.db`
   - confirm logs move to the new checkout paths.

### Gate E — Phase 5: nginx (needs **sudo**, interactive auth)
10. `sudo cp $HOME/deploy/main/app/deploy/nginx-staging.conf /etc/nginx/sites-available/muscat-staging`
11. Symlink into `sites-enabled`, `sudo nginx -t`, `sudo systemctl reload nginx`.
12. Verify `:8002` reverse-proxies to `:8003` and serves the staging app.

### Gate F — Phase 7: Actions vars + secrets (**org admin**)
13. Set `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` **vars** (after Phases 1–6
    verified, per issue ordering) into both branches' environments.
14. Bootstrap staging: `git push` to `test` → staging uvicorn :8003 in tmux
    `muscatdb-test`.
15. Verify :8003 (via :8002) + `/healthz`, then set the `DEPLOY_*` **secrets** for both.

### Final verification (from the plan)
- `:8001` / `:8003` refuse unauthenticated `/` (401); `/healthz` → 200.
- Staging `build-db` never touches prod `muscat.db`.
- Prod `:8001` and prod cron both healthy on the new checkout.
- Close issue #26 by hand.

**Live-verification reminders:** after deploy the server runs via bare `uvicorn`
(no `--reload`); HTML/JS changes need a server restart to show. Server runs in tmux
`muscatdbgui` (prod) / `muscatdb-test` (staging).
