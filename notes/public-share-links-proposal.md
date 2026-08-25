# Proposal: public share links for photometry / transit-fit / TTV-fit results

> Status: proposal, not yet implemented. Written 2026-08-25 against `test`@`3c789c0`; `test` has since moved (`docs/` was renamed to `notes/` and several of the referenced templates/`web.py` changed upstream), so file:line references below should be re-checked against current `test` before implementation, not assumed exact.

## Context

Collaborators need a fast way to hand someone a link to a specific run's results (plots, light curves, fit summaries) without giving them SSH access or Basic Auth credentials to muscat-db. The request named three candidate solutions to evaluate: NextCloud AIO, copyparty, and "other alternatives."

Two facts from the codebase make the answer fall out cleanly:

1. **All three result pages already share one zip-building backend.** `photometry.html`, `transit_fit.html`, and the TTV section of `ephemeris.html` each already have a "📦 Download all output (.zip)" button wired to a per-kind `download-all` route (`web.py:2762` etc.), all of which funnel into one shared helper, `_create_zip_response` (`web.py:2639-2717`) — content-fingerprint caching, file-count/size caps, disk-space checks, and a build-concurrency semaphore are already handled there. There's also an existing "unauthenticated slug → stored state" precedent: `ephemeris_views` (`database.py:261-268`, routes at `web.py:3245-3264`).

2. **muscat-db has zero public network exposure today.** It's reachable only via `ssh -L 8000:localhost:8000` to a loopback-bound nginx with Basic Auth (`deploy/nginx.conf`, `notes/DEPLOYMENT.md`). No domain, no TLS, no firewall rule, no Docker on the host at all.

Given fact 2, the request's own reference links are more informative for *how they solve exposure* than for the app they wrap: the NextCloud AIO discussion ([nextcloud/all-in-one#6817](https://github.com/nextcloud/all-in-one/discussions/6817)) is itself a community workaround because containerizing Tailscale as AIO's default reverse-proxy method was "unnecessarily complex" — and the [ScaleTail copyparty example](https://github.com/tailscale-dev/ScaleTail/tree/main/services/copyparty) runs a single lightweight binary behind a Tailscale sidecar. Both point at the same underlying mechanism (**Tailscale**) rather than at the wrapping app being the important part.

Talked through with the user:
- The data should stay **hosted in muscat-db itself**, not pushed out to a third party — rules out Google Drive and rules out standing up a second file-server app (copyparty) or a groupware suite (NextCloud AIO) to hold a copy of the data. muscat-db already has the files and already builds the zip; nothing else should own a second copy.
- The goal is "share results quickly to anyone with the link" — an unlisted-link model (like the existing `ephemeris_views` precedent), not an account/login system for viewers.
- **Tailscale Funnel** (not Serve) is the exposure mechanism: Funnel publishes a normal public HTTPS URL on a `*.ts.net` domain — viewers need no Tailscale client or account, only the host (muscat-ut2) needs to join a tailnet. This avoids both a university-IT firewall request and standing up Let's Encrypt/certbot on a host that's never needed a public cert before.

**Recommendation: extend muscat-db's own zip/share code with a new, narrowly-scoped public share-link feature, exposed only via Tailscale Funnel on a dedicated port.** No new services (rules out copyparty and NextCloud AIO), no data leaving the host (rules out Google Drive), reuses the zip infra that already exists and is already hardened.

## Design

### Data model — new `shared_artifacts` table (`database.py`, near `ephemeris_views` at line 261)

```sql
CREATE TABLE IF NOT EXISTS shared_artifacts (
    slug             TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,       -- 'photometry' | 'transit_fit' | 'ttv_fit'
    params_json      TEXT NOT NULL,       -- lookup params to rebuild the run dir (never trust client-supplied path segments on the public route)
    created_by       TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at       TEXT,
    revoked_at       TEXT,
    hit_count        INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT
);
```

Unlike `ephemeris_view_slug()` (`database.py:380-386`), which is a **deterministic content-hash** (same input state → same slug — fine for an idempotent view-state permalink, wrong for something that gates file access), mint slugs with `secrets.token_urlsafe(16)` (~128 bits) so they're unguessable. Add `mint_shared_artifact(kind, params, created_by) -> dict`, `get_shared_artifact(slug) -> dict | None`, `revoke_shared_artifact(slug)`, `record_share_access(slug)` alongside `save_ephemeris_view`/`get_ephemeris_view` (`database.py:1843-1883`) as the style template. Check `fov.html:841`'s existing "shareable link" reference during implementation in case there's a closer precedent to reuse.

### Backend routes (`web.py`)

- **Mint (authenticated)**: `POST /api/{photometry|transit-fit|ttv-fit}/share/...` taking the same params as each kind's existing `download-all` route. Validate exactly like those routes already do (`inst in INSTRUMENTS`, `phot.valid_date(date)`, run_id sanitization), confirm the run directory actually resolves and has files, then `mint_shared_artifact(...)` and return `{"slug": ..., "url": f"{MUSCAT_SHARE_PUBLIC_BASE_URL}/share/{slug}"}`.
- **Public view (unauthenticated)**: `GET /share/{slug}` — look up the slug, 404 on missing/revoked/expired, re-resolve the run directory **from the stored `params_json`** (never from the URL) using the same per-kind resolvers already in use (`fit.fit_output_dir` for transit fit, the photometry equivalent, TTV's run-dir resolver), and render a minimal public template (new `templates/share_view.html`, following `styles.css` per the project's design-consistency convention, no nav/auth chrome): inline `<img>` for PNG/GIF plots, a file table with individual download links, and a "📦 Download all (.zip)" link. Bump `hit_count`/`last_accessed_at`.
- **Public file/zip (unauthenticated)**: `GET /share/{slug}/file/{name}` and `GET /share/{slug}/download-all.zip`, reusing the existing path-sanitization helpers (the `safe_*_path` family) and `_create_zip_response` (`web.py:2639`) — same caching/size/concurrency protections that already exist for the authenticated download-all routes.
- **Revoke (authenticated)**: small endpoint + a "My shared links" panel (or a row action from the pages that minted them) so a lab member can kill a link without touching SQLite directly.

### Auth middleware exemption

Confirmed: enforcement is a **single global middleware**, `_nginx_auth_middleware` (`web.py:260-299`), which protects every path except `/healthz` and `/static/` via `protected = not (...)`. Add the `/share/` prefix to that exemption — this is the one place a new route can opt out of auth; there's no per-router dependency to bypass separately.

### Network exposure — dedicated port, not the main app port

nginx today has exactly **one** `server` block with `auth_basic` set at server scope, covering `location /` and `location /socket.io` (`deploy/nginx.conf`) — there is no existing precedent for a differently-authed route in this config (the `/tess-quicklook` companion app is *not* a separate nginx location; it's just another FastAPI router in the same authenticated app).

Do **not** point Tailscale Funnel at the existing app port. Even though `/` would still demand Basic Auth, that would put the login prompt itself on the public internet for the first time — a bigger blast-radius change than intended. Instead:

1. Add a **new nginx `server` block on its own loopback port** (e.g. `127.0.0.1:8020`) containing only `location /share/ { auth_basic off; proxy_pass http://127.0.0.1:8001; ... }` plus `limit_req`/`limit_conn` zones, and a bare `return 404;` for anything else on that port.
2. `tailscale funnel` that dedicated port only, so the worst case of any Funnel/ACL misconfiguration is over-exposing an already-public-by-design route — the authenticated app's login surface stays exactly as unreachable from the internet as it is today (SSH-tunnel only).
3. New config: `MUSCAT_SHARE_PUBLIC_BASE_URL` (the `*.ts.net` URL, used to build the links returned by the mint endpoint), documented alongside the other env vars in `config.py`.
4. Record the new port, Funnel setup, and share-link lifecycle in `notes/DEPLOYMENT.md`, per the project's convention of recording infra/deployment changes there.

Decide (flag for the user, don't assume): default `expires_at` — e.g. 90 days with manual revoke available anytime — vs. no expiry at all until manually revoked. "Share quickly" doesn't imply "forever," so a default TTL with revoke-anytime is the safer default; make it a config constant (`MUSCAT_SHARE_DEFAULT_TTL_DAYS`) so it's easy to change without a code change.

### Frontend

Add a "🔗 Share" control as a sibling of the existing download-all link in each page's `.dl-row`-equivalent block:
- `photometry.html` (~line 301-302)
- `transit_fit.html` (~line 345-347): `<a href="{{ dl_all_url }}" download>📦 Download all output (.zip)</a>` — add `<a id="share-btn">🔗 Share</a>` next to it.
- TTV section of `ephemeris.html` (~line 3945-3954)

Click handler: `fetch(...)` POST to the mint endpoint, then show the returned URL in a copy-to-clipboard toast/modal — check for an existing toast/modal helper in the shared JS/CSS before writing a new one, to stay consistent with the rest of the app.

## Phasing

**Phase 1 (code only, safe to build and test entirely over the existing SSH tunnel — no new exposure yet):** DB table, mint/view/file/zip routes, middleware exemption, frontend buttons, public template. Validate the whole flow through the tunnel first, exactly like `ephemeris_views` already works today, before anything is internet-reachable.

**Phase 2 (operational, needs the user's hands-on involvement — do not run unattended):** Install Tailscale on muscat-ut2, join/create a tailnet, add the dedicated nginx server block, run `tailscale funnel` for that port, set `MUSCAT_SHARE_PUBLIC_BASE_URL`. This needs an interactive `tailscale up` login and is a real change to the server's network posture, so it should be a deliberate, separate step the user confirms live rather than something bundled into the same PR as the code.

## Verification

- Unit tests for `mint_shared_artifact`/`get_shared_artifact`/`revoke_shared_artifact` (follow existing test file naming near the photometry/transit-fit web tests; locate via Grep for `test_web_*` patterns).
- Route tests: mint without auth → 401; public `GET /share/{slug}` works with no auth; unknown/revoked/expired slug → 404; attempts to smuggle path traversal via stored params rejected by the existing `safe_*_path` validators (same as today's authenticated routes).
- Manual: over the SSH tunnel, mint a link from each of the three pages, open the returned `/share/{slug}` URL in a private/incognito browser window (no cookies/auth), confirm plots render inline and the zip downloads correctly.
- After Phase 2: confirm the dedicated port is Funnel-reachable from an actual external network (e.g. mobile data, not on the lab LAN) and that the **main app port remains unreachable** from the same external network (this is the property the dedicated-port design exists to guarantee — verify it, don't assume it).
