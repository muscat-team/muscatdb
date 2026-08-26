# Target Tagging & Project Management (Issue #88)

> This document originally drafted the design before implementation. It has
> been rewritten to reflect what was actually built — the original draft keyed
> `target_tags` on the raw `object` string and placed "Projects" before a
> "Target" nav item that no longer exists; both were corrected before
> implementation. See the PR/commits for this feature for the authoritative
> code; this file is a summary, not a spec to implement against.

## Key design decision

`target_tags` keys on `norm_name` (the normalized target identity used by
`/target?name=<norm_name>`), not the raw obslog `object` string. Multiple raw
`object` rows can normalize to the same `norm_name` (see
`_normalize_target_name` in `catalog.py`), so tagging by `norm_name` groups
every raw-object spelling variant of one physical target under the same set of
project tags — consistent with how `/target` already aggregates them.

## Schema (`database.py`)

```sql
CREATE TABLE IF NOT EXISTS tag_descriptions (
    tag         TEXT PRIMARY KEY COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS target_tags (
    norm_name  TEXT NOT NULL,
    tag        TEXT NOT NULL COLLATE NOCASE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (norm_name, tag)
);
```

Both are registered in `_APP_OWNED_TABLES` so they survive the nightly
`build_db` rebuild.

## Routes

- Pages: `GET /projects` (directory), `GET /tags` (redirects to `/projects`),
  `GET /tag?name=<project>` (detail; renders a friendly not-found state at 200
  for an unknown project rather than a hard 404).
- API: `GET/POST /api/tags`, `PUT/DELETE /api/tags/{tag}`,
  `GET /api/tags/{tag}/export.csv`, `GET/PUT/DELETE /api/targets/{obj}/tags`
  (DELETE takes `{"tag": ...}` in the body, not a `/tags/{tag}` path segment),
  `GET /api/targets/norm-names`. Every `{obj}` path param is normalized via
  `_normalize_target_name` before touching `target_tags`.

## Navbar

"Projects" sits immediately after "ObsLog" (there is no standalone "Target"
nav item — `Targets` at `/targets` is the item right before ObsLog).

## Homepage (`index.html`)

Dropped RA, Dec, Airmass, and Move (the Identify/Unidentify toggle) columns;
added a Tags column (chips + a "+ Tag" quick adder that only attaches
*existing* projects). `/api/targets/export.csv` keeps its existing columns and
gains a trailing `tags` column.

## Target back-link

`target.html`'s back-link is client-side only: it reads
`?from=project&project=<name>` from `window.location.search` and swaps its
label/href accordingly, so `/target`'s server-side HTML cache needed no
changes.
