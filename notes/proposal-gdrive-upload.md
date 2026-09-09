# Proposal: Upload pipeline output ZIPs to the user's Google Drive

## Background

After running full photometry, transit fit, or TTV fit, the GUI already has a
manual "Download all output (.zip)" button on the photometry, transit-fit,
and ephemeris pages. This proposes adding the ability to upload that same
ZIP to the user's own Google Drive, either on demand or automatically when a
job finishes.

## Decisions already made

1. **Auth approach: OAuth user consent.** Uploads land in the user's own
   Google Drive under their own quota. Not a service account, not an Apps
   Script relay, not rclone.
2. **Trigger UI:** an on-demand "Upload to GDrive" button on each of the
   photometry page, transit-fit page, and ephemeris page — the same pages
   that already have the manual zip-download button.
3. **Settings page:** a manual/auto toggle. In manual mode, upload only
   happens on click. In auto mode, the zip is built and uploaded
   automatically as soon as the corresponding job reaches a terminal
   success state.

Rejected alternatives and why: a service account avoids per-user OAuth but
uploads land in the service account's own storage, not the user's Drive; an
Apps Script relay keeps all Google credentials out of muscat-db but makes
the feature depend on a separately-hosted, separately-maintained script;
rclone is a good fit for a headless single-operator setup but is a
server-level config rather than something a user connects from the GUI.

## Phase 0 — Human prerequisite (not automatable)

One Google Cloud project for the whole muscat-db deployment, set up once by
hand before any code lands:

1. Create/choose a project at console.cloud.google.com.
2. Enable the Google Drive API.
3. OAuth consent screen: User type **External**, app name `muscat-db`,
   support/developer email.
4. Scopes: add **only** `https://www.googleapis.com/auth/drive.file`
   ("see, edit, create, and delete only the specific files you use with
   this app"). Do not add `.../auth/drive` or `drive.readonly`. `drive.file`
   is in Google's non-sensitive tier, so no verification review is needed.
5. **Publish to "In production"** (Audience → Publish app). This is the
   single most important step: while the app is in Testing, Google expires
   every refresh token after 7 days, so every connected user silently loses
   their connection weekly. The narrow scope means publishing needs no
   verification.
6. Credentials → Create OAuth client ID → Web application. Authorized
   redirect URIs — add both deployment ports (users reach the app over an
   SSH tunnel at `localhost`, and Google permits plain `http` only for
   loopback):
   - `http://localhost:8000/api/settings/gdrive-callback` (production)
   - `http://localhost:8002/api/settings/gdrive-callback` (staging)
7. Record the Client ID + Client Secret into each checkout's gitignored
   `.env`, never into the repo:
   ```
   MUSCAT_GDRIVE_CLIENT_ID=...apps.googleusercontent.com
   MUSCAT_GDRIVE_CLIENT_SECRET=...
   MUSCAT_GDRIVE_REDIRECT_URI=http://localhost:8000/api/settings/gdrive-callback   # :8002 on staging
   ```
8. `MUSCAT_DB_SECRET` must already be set and stable — it is the Fernet key
   that encrypts the stored refresh token.

Constraint to document for users: the redirect URI is fixed per deployment,
so the SSH tunnel must use the standard local port. A user tunneling to a
different local port cannot complete the consent flow — call this out in
the Settings hint text and README.

## Architecture

### Dependency choice: hand-rolled httpx, not `google-api-python-client`

Every external integration in this repo (LCO, ESO OIDC, ADS, Google Sheets
gviz) is already a direct `httpx` call. `google-auth-oauthlib` +
`google-api-python-client` would pull in `oauthlib`, `httplib2`,
`uritemplate`, `protobuf`, `googleapis-common-protos` for what is three REST
endpoints: the OAuth authorize redirect (no client code needed), the token
endpoint (exchange + refresh), the revoke endpoint, and the Drive v3
resumable-upload/files endpoints. Hand-roll in `gdrive.py` on
`http_client.get_sync_client()` / `get_async_client()`. Fallback if refresh
edge cases become a burden: add `google-auth` alone (no `-oauthlib`, no
`api-python-client`) for `Credentials.refresh()`.

### Uploads must be resumable and backgrounded

`_ZIP_MAX_INPUT_BYTES` defaults to 2 GiB. Google's simple/multipart upload
caps at 5 MB, so resumable upload is mandatory: POST metadata, read the
`Location:` session URI, `PUT` in 8 MiB chunks (multiple of 256 KiB except
the last) with `Content-Range`. Chunking also gives free progress reporting
and per-chunk retry.

A multi-minute upload must never occupy a FastAPI threadpool slot or the 2s
job-reconciliation thread. Mirror `lco.py`'s archive-download design: a
module-level upload-job registry + `ThreadPoolExecutor` + lock + TTL
pruning + per-user active cap. Routes return 202 + `job_id`; the browser
polls.

### File layout — do not grow `web.py` further

`web.py` is already ~6800 lines. New code goes in new modules; `web.py`
gets thin handlers only.

| New file | Contents |
|---|---|
| `src/muscat_db/gdrive.py` | OAuth URL build, code exchange, refresh, revoke, folder resolve/create, resumable upload, upload-job registry, the `fire_job_finished` hook. Module shape mirrors `gsheet_ephemeris.py`. |
| `src/muscat_db/archives.py` | The three `files_to_zip` manifest builders extracted from `web.py`, plus `build_archive()` (the zip-building half of `_create_zip_response`). |

Import graph (no cycles): `archives` → `photometry`/`transit_fit`/`ttv_fit`;
`gdrive` → `archives`, `database`, `http_client`, `jobs`; `web` → both.
`jobs.py` stays stdlib-only.

## Data model

All in the existing `users.settings` JSON blob via
`database.update_user_settings`, following the pattern already used for the
ephemeris-sheet settings:

| Key | Encrypted? | Meaning |
|---|---|---|
| `gdrive_refresh_token_enc` | yes (Fernet, `MUSCAT_DB_SECRET`) | long-lived refresh token |
| `gdrive_account_email` | no | connected Google account, display only |
| `gdrive_folder_id` | no | Drive folder ID the app created |
| `gdrive_folder_name` | no | default `"muscat-db"` |
| `gdrive_auto_upload` | no | bool; false = manual |
| `gdrive_pending_state` | verifier field encrypted | `{state, verifier_enc, expires_at}`, single-use, 10 min TTL |
| `gdrive_needs_reconnect` | no | set when a refresh returns `invalid_grant` |

New accessors in `database.py`, alongside `set_user_eso_credentials`:
`set_user_gdrive_token` / `get_user_gdrive_token` / `user_gdrive_configured`,
`set_user_gdrive_prefs` / `get_user_gdrive_prefs`,
`set_user_gdrive_pending_auth` / `take_user_gdrive_pending_auth` (pop-once).

Access tokens are never persisted — they live for ~1h in a process-local
dict in `gdrive.py`, refreshed on demand. Only one long-lived secret is
stored at rest.

## Backend design

### `gdrive.py` public surface

```
class GdriveError(RuntimeError)
class GdriveAuthError(GdriveError)       # invalid_grant / revoked -> reconnect required

client_configured() -> bool
authorization_url(user) -> str           # mints+persists state + PKCE verifier
exchange_code(user, code, state) -> dict # validates state, stores refresh token + email
disconnect(user) -> None                 # POST /revoke, then clear stored keys
connection_status(user) -> dict          # no token material in the return value

resolve_folder_id(user) -> str           # find-or-create, handles deleted/trashed folder
start_upload(user, *, files, archive_name, source) -> dict   # 202 snapshot
upload_status(job_id, user) -> dict
```

OAuth details that must be got right:
- Authorization URL params: `client_id`, `redirect_uri` (read from
  `MUSCAT_GDRIVE_REDIRECT_URI`, never derived from the request Host header),
  `response_type=code`, `scope=https://www.googleapis.com/auth/drive.file`,
  `access_type=offline`, `prompt=consent` (without it Google omits
  `refresh_token` on re-consent), `include_granted_scopes=true`, `state`,
  `code_challenge` + `code_challenge_method=S256`.
- `state` verified with `hmac.compare_digest`, popped single-use,
  expiry-checked. The callback re-derives the user from the auth proxy, so a
  state minted for one user can never be redeemed by another.
- Refresh: `POST /token` with `grant_type=refresh_token`. On `invalid_grant`
  raise `GdriveAuthError`, set `gdrive_needs_reconnect`, and do not delete
  the row (so the UI can say "reconnect" rather than silently forgetting).
- Retry/backoff: exponential backoff with jitter on HTTP 429/500/502/503/504
  and `rateLimitExceeded`/`userRateLimitExceeded`; max 5 attempts; never
  retry 400/401/403-`insufficientPermissions`.

Idempotency: before uploading, `files.list` scoped to the app's own folder
(with `drive.file` this only ever sees files this app created). If present,
`PATCH` a new revision instead of creating a duplicate — a double click or a
hook re-fire is harmless.

### Refactor of the existing zip path (do first)

`_create_zip_response()` currently does manifest validation, fingerprint
cache, build, and `FileResponse` in one function. Split it:

- `archives.build_archive(files_to_zip, *, block: bool = False) -> Path` —
  returns the cached `.zip` path. `block=True` for the background
  auto-upload path, so it queues instead of raising the interactive 429.
- `web._create_zip_response()` becomes a thin wrapper around it. Behaviour
  for existing download links is unchanged.
- Extract the three manifest builders verbatim into `archives.py`:
  `photometry_manifest(...)`, `transit_fit_manifest(...)`,
  `ttv_manifest(...)`, each returning `(files_to_zip, archive_name)`. The
  existing download routes call them too, so download and upload can never
  drift apart.

### New routes

In `settings_router`, following the existing status/save/test shape used by
the LCO/ESO/ADS/ephemeris-sheet settings:

| Method | Path | Notes |
|---|---|---|
| GET | `/api/settings/gdrive-status` | `{ok, connected, account_email, folder_name, auto_upload, client_configured, needs_reconnect}` — never a token |
| POST | `/api/settings/gdrive-connect` | returns `{ok, authorize_url}`; JS redirects the browser |
| GET | `/api/settings/gdrive-callback` | Google's redirect target; redirects to `/settings?gdrive=connected` or `?gdrive=error&reason=...` |
| POST | `/api/settings/gdrive-disconnect` | revoke + clear |
| POST | `/api/settings/gdrive-mode` | `{mode: "manual"|"auto"}` |
| POST | `/api/settings/gdrive-folder` | `{folder_name}`, default `"muscat-db"` |

Per-page triggers, registered next to each existing `download-all` route so
the path shapes match one-for-one:

```
POST /api/photometry/upload-gdrive/{inst}/{date}/{target}/run/{run_id}
POST /api/photometry/upload-gdrive/{inst}/{date}/{target}
POST /api/transit-fit/upload-gdrive/{inst}/{date}/{target}/run/{run_id}
POST /api/transit-fit/upload-gdrive/{inst}/{date}/{target}
POST /api/ttv-fit/upload-gdrive?target=&run_name=
```

New `gdrive_router = APIRouter(prefix="/api/gdrive")`:

```
GET /api/gdrive/upload-status?job_id=...   # snapshot; 404 if not the caller's
GET /api/gdrive/uploads                    # this user's recent uploads
```

### Auto-upload hook point

Hook into `jobs.register_job_finished_hook` / `jobs.fire_job_finished` —
called from `sync_jobs` after `resolve_job_state` has cleared the
`finalizing` grace window and the row is persisted, and already dedups per
`job_key`. This is exactly the "genuinely terminal" point that must be used
rather than raw process-exit, per the pipeline's finalizing-state-machine
design.

```python
def on_job_finished(job_key, type_="", target="", inst="", date="",
                    state="", run_id="", run_name="", **_):
    if state != "done":
        return                       # error/cancelled never upload
    user = database.get_job_user_name(job_key) or ""
    if not user or not get_user_gdrive_prefs(user).get("auto_upload"):
        return
    _UPLOAD_EXECUTOR.submit(_auto_upload, user, type_, inst, date, target, run_id, run_name)

jobs.register_job_finished_hook(on_job_finished)
```

Four things this must respect:
1. Never block the sync thread — the hook only submits to the executor.
2. Never fail the job — the upload worker gets its own try/except; the
   pipeline result is untouched either way.
3. `ttv_fit.py` does not call `fire_job_finished` today. Add the call,
   passing `run_name`. This has a side effect worth flagging in the PR: TTV
   jobs will start producing chat notifications too (the existing chat
   consumer of this hook needs a `"ttv_fit"` label added).
4. Hook registration must live in every process that runs `sync_jobs`,
   including any standalone worker process, not just the main web process.

## Frontend design

### Three page buttons

Same visual language as the existing download link — an entry in the same
row using the existing accent color and a `.lco-msg`-style status span.
Shared JS helper (in the shared base template so all three pages get one
implementation):

```
idle      -> "Upload to Google Drive"
posting   -> button disabled, "Preparing archive..."
zipping   -> "Zipping output..."
uploading -> "Uploading... 42%"   (poll upload-status every 2s)
done      -> ok message "Uploaded" + link to the file in Drive
error     -> err message with the server-provided reason
not connected (409)  -> err message + link to Settings
needs reconnect       -> err message, "access expired - reconnect in Settings"
```

Constraints from this repo: no `alert()`/`confirm()`/`prompt()` (blocked by
an existing frontend audit test) — use the existing modal/message-span
pattern instead. Percent display integer-only. The button is a `<button>`,
not an `opt-` input, so it is intentionally outside the
collectOptions/restoreOptions localStorage contract.

### Settings page

New collapsible section on the settings page, after the existing
credential sections, following the same markup shape (heading toggle +
body + actions + status line):

```
Google Drive
  [Connect Google Drive] / [Disconnect]      <- swaps on connection state
  Upload mode:  ( ) Manual - only when I click "Upload to Google Drive"
                ( ) Automatic - upload as soon as a job finishes successfully
  Folder name: [muscat-db]                    <- created in your Drive on first upload
  hint: muscat-db only ever sees files it created in your Drive.
        Connecting requires the standard tunnel port.
  status: Drive: connected as a***@gmail.com | mode: manual | folder: muscat-db
```

The manual/auto toggle is server-side only, deliberately not in
localStorage — it has to steer unattended backend behaviour from the
job-sync thread, where no browser state exists.

## Error handling and security

| Concern | Handling |
|---|---|
| Refresh token at rest | Fernet-encrypted with `MUSCAT_DB_SECRET`, same as other stored credentials. Decrypt failure -> 503 "stored Drive token cannot be read" |
| Never logged | No token, code, verifier, or Authorization header value in any log line or exception message |
| Not exposed to the browser | status endpoint returns booleans + a masked email only; client secret marked `secret=True` in the config registry so it's excluded from any startup report |
| Revoked / expired | `invalid_grant` -> keep the row, set `needs_reconnect`, surface "reconnect required" in Settings and on all three page buttons; manual upload returns 409 |
| Auto-upload failure | caught in the worker, recorded in the upload snapshot, logged; the pipeline job's own state is never touched |
| Rate limiting / abuse | per-user cap on concurrent uploads, bounded executor width, backoff-with-jitter on Google 429/5xx |
| SSRF / open redirect | all Google hostnames are module constants; nothing user-supplied ever becomes a request host; the post-callback redirect target is a hardcoded literal, never state-carried |
| CSRF | all mutating routes are POST behind the existing same-origin gate; the GET callback carries the single-use `state` plus the authenticated user |
| Cross-user access | upload-status compares the snapshot owner against the requesting user and 404s otherwise |

New `config.py` env vars (must be registered in the existing env-var
registry, with the client secret marked as a secret):

```
MUSCAT_GDRIVE_CLIENT_ID
MUSCAT_GDRIVE_CLIENT_SECRET        (secret)
MUSCAT_GDRIVE_REDIRECT_URI
MUSCAT_GDRIVE_FOLDER_NAME          default: muscat-db
MUSCAT_GDRIVE_UPLOAD_WORKERS       default: 2
MUSCAT_GDRIVE_MAX_ACTIVE_PER_USER  default: 2
MUSCAT_GDRIVE_CHUNK_BYTES          default: 8388608
MUSCAT_GDRIVE_TIMEOUT_S            default: 120
MUSCAT_GDRIVE_JOB_TTL_S            default: 86400
MUSCAT_GDRIVE_AUTH_STATE_TTL_S     default: 600
```

## Testing plan

Follow the shape of the existing ephemeris-sheet settings tests (DB
accessors + HTTP routes + authed-client fixture).

`test_gdrive_oauth.py` (unit, network fully mocked):
- authorization URL requests offline access and forces consent, with
  exactly the `drive.file` scope
- authorization URL uses the configured redirect, not the request host
- callback rejects a state minted for another user
- callback rejects an expired state / state is single-use
- refresh token is encrypted at rest
- `invalid_grant` marks needs-reconnect without dropping the row
- status payload contains no token material

`test_gdrive_upload.py` (unit, mocked HTTP transport):
- large archive uses a resumable session, not multipart
- chunk size is a multiple of 256 KiB
- transient 5xx is retried; 429 backs off
- an existing same-name file is updated in place, not duplicated
- a missing/trashed folder is recreated
- upload status is not readable by another user

`test_gdrive_settings_routes.py` (integration):
- status/connect/mode/folder/disconnect round-trip
- status requires authentication
- mutating routes rejected cross-origin
- upload returns 409 when Drive is not connected

`test_gdrive_auto_upload.py` (hook wiring):
- hook fires only on `done`, not error/cancelled
- hook is a no-op in manual mode
- hook returns immediately and never blocks the sync thread
- an upload failure never changes the job row
- TTV `sync_jobs` fires `job_finished` (new behaviour)

Refactor-safety tests, written before the `archives.py` extraction and
required to still pass unchanged after: the download-all routes for all
three pipelines still serve byte-identical manifests.

Red-green discipline (per this repo's testing rule) applies to the two
genuinely bugfix-shaped edge cases:
- access token expires mid-upload, between two chunk PUTs — write the test
  first, confirm it fails with the mid-upload refresh path removed, then
  restore and confirm it passes
- a replayed OAuth callback (state reuse) — confirm it fails without the
  pop-once guard, passes with it

Every new test must run with no network and no `MUSCAT_GDRIVE_*` set (the
routes degrade to a clean 503), so nothing hangs in CI. Nothing here is
`@pytest.mark.slow`.

Manual verification after deploy: on staging, connect, run a short test
photometry job in auto mode, confirm the ZIP appears in Drive; disconnect,
click the manual button, confirm the "not connected" message links to
Settings. Template/JS changes need a server restart to take effect (the
reload watcher only tracks Python files).

## Task breakdown — 4 independently-mergeable PRs into `test`

Short-lived branches off `test`, not stacked — wait for each to land before
branching the next.

**PR 1 — `refactor/extract-archive-builders`** (no user-visible change)
New `archives.py` with `build_archive()` and the three manifest builders,
moved verbatim out of `web.py`; `_create_zip_response` becomes a thin
wrapper. Tests: manifest-parity tests. Risk: low — pure extraction.

**PR 2 — `feat/gdrive-oauth-connection`** (backend + Settings UI, no
uploads yet)
Config/env registry, `database.py` accessors, `gdrive.py` OAuth half,
six settings routes, Settings page section. Tests: OAuth unit tests +
settings-route integration tests. Risk: medium — OAuth state handling and
secret-at-rest are the security-sensitive parts; worth a dedicated security
review pass.

**PR 3 — `feat/gdrive-manual-upload`** (the three page buttons)
Folder resolve/create, resumable upload, upload-job registry, five
per-page routes + status/history routes, shared JS helper, the three page
buttons. Tests: upload unit tests including the red-green mid-upload-token
case. Depends on PR 1 + PR 2. Risk: medium — long-running I/O and
resumable-upload correctness.

**PR 4 — `feat/gdrive-auto-upload`** (the settings toggle goes live)
`on_job_finished` hook registration, `ttv_fit.py` gains its
`fire_job_finished` call, chat label addition. Tests: hook-wiring tests.
Depends on PR 3. Risk: medium-high — the one PR that touches the job
lifecycle; review checklist: no inline I/O in the hook, only `done` state
uploads, upload failure cannot mutate the job row.

## Risks and mitigations

- Refresh tokens expire after 7 days if the consent screen isn't published
  to production — mitigated in Phase 0; `needs_reconnect` surfaces the
  failure within one page load rather than after a week of silent misses.
- Redirect URI mismatch for a user tunneling to a non-standard local port —
  not solvable in code; documented in the Settings hint and README.
- A 2 GiB upload holding a worker for a long time — bounded background
  executor, per-user cap, chunked/cancellable progress.
- ZIP cache and Drive upload competing for temp-dir space — the upload
  streams from the already-cached zip file; no second copy is made.
- Auto-upload racing the finalizing grace window — mitigated by hooking
  `fire_job_finished` rather than raw process exit.
- Duplicate uploads on a hook re-fire after a restart — same-name update-
  in-place instead of create.
- `web.py` growth — all new logic lives in `gdrive.py`/`archives.py`;
  `web.py` gains only thin handlers, and PR 1 nets it smaller.

## Success criteria

- [ ] A user connects Google Drive from Settings; the consent screen
      requests only "specific files you use with this app".
- [ ] In manual mode, the button on each of the photometry / transit-fit /
      ephemeris pages uploads the same ZIP the download link produces, with
      visible progress and a working Drive link on success.
- [ ] In auto mode, a successful job's ZIP appears in Drive with no click;
      a failed or cancelled job uploads nothing.
- [ ] A failed or expired Drive upload never changes a pipeline job's
      state, log, or outputs.
- [ ] Disconnect revokes at Google and clears the stored token; a
      subsequent upload returns a clean "not connected" message.
- [ ] No token, client secret, code, or verifier appears in any log, API
      response, or template.
- [ ] Tests pass, lint is clean, new coverage >= 80% on `gdrive.py` and
      `archives.py`, both red-green cases documented in their PR bodies.
