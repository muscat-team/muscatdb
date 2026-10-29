# muscatdb

<!--
#[![Documentation](https://readthedocs.org/projects/quicklook/badge/?version=latest)](https://quicklook.readthedocs.io/en/latest/)
#[![PyPI](https://img.shields.io/pypi/v/muscat-db)](https://pypi.org/project/muscatdb/)
#[![Python](https://img.shields.io/pypi/pyversions/muscat-db)](https://pypi.org/project/muscatdb/)
-->
[![CI](https://github.com/muscat-team/muscatdb/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/muscat-team/muscatdb/actions/workflows/ci.yml)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/muscat-team/muscatdb)

`muscat-db` is a web-based, closed-loop exoplanet observing and analysis workflow
that integrates a central database with semi-automated, reproducible photometry,
transit, ephemeris, and TTV analyses, enabling efficient LCO scheduling with
optional FOV and exposure-time optimization and integration with external TOI
and NExScI catalogs. Supported workflow pages expose a unique, shareable view state,
allowing configurations, selections, and results to be reviewed transparently,
reproduced consistently, and communicated easily among collaborators.

## Pipeline engines

muscat-db orchestrates three external packages. Development uses editable
`ext_tools/` checkouts in dedicated conda environments. Production uses exact
commits installed in separate environments, selected by `deploy/engines.toml`
and `deploy/workers.toml`. muscat-db stores their inputs, launches them as
background jobs, and renders their outputs; the science lives in the packages.

The photometry pin is prose2 3.5.1. It corrects exposure-midpoint timestamps and
measurement uncertainties, and drops frames with invalid target measurements.
Existing reductions and transit fits keep their original results; rerun them
under a new run name to obtain the corrections while preserving the comparison.
CSV/NPZ formats and existing command options remain compatible. The new
`--workers`, `--chunksize`, and `--plots` options retain prose2's defaults in the
web interface. See [the engine deployment procedure](notes/DEPLOYMENT.md)
for coordinated web/worker activation and rollback.

| Stage | Engine | Sampler | Outputs |
|---|---|---|---|
| Photometry | [`prose2`](https://github.com/jpdeleon/prose2) | — | Lightcurve CSVs, diagnostics |
| Transit fitting | [`timer`](https://github.com/john-livingston/timer) | PyMC (NUTS) | Posteriors, corner/trace/fit plots |
| TTV fitting | [`harmonic`](https://github.com/john-livingston/harmonic) | emcee | Posteriors, corner/trace/fit plots |

When scheduling from a saved harmonic fit, `fit_config.json`'s `t_offset`
converts between the fit's time system and absolute BJD, including when extending
the predictions to the requested end date. Successful model, ΔBIC, and ranking
results are cached for five minutes; failed evaluations can be retried immediately.
Invalid ranking uncertainties leave the transit windows available with a ranking
error instead of failing the scheduling request.

The schedule page can also take mid-transit times from outside muscat-db: choose
**Uploaded mid-times** as the ephemeris source and paste or load a list (full
BJD_TDB or ISO UTC per line, optionally `tc_err` in days, separated by commas or whitespace). Each time is
scheduled as given and gets the same per-site rating as a generated window; the
**Show** filter selects full, partial, split or unobservable transits. A list
with any malformed line, or more than 1000 times, is refused with the line
numbers at fault.

`MUSCAT_MAX_FULL_JOBS` controls the concurrent full-run cap per pipeline (default
`1`; `0` disables full runs). Empty or malformed values use `1`, with a warning
for non-empty malformed values. Negative integers retain the disabled (`0`)
behavior.

## Requirements

- Python ≥ 3.12
- FITS files below the common data root (`MUSCAT_DATA_DIR`, default `/data`) in
  `MuSCAT`, `MuSCAT2`, `MuSCAT3`, `MuSCAT4`, `Sinistro`, `SBIGSTL6303`, and
  `QHY600CMOS` subdirectories
- A writable obslog directory (`MUSCAT_OBSLOG_DIR`, default
  `$HOME/muscat/obslog`)

## Instruments

| Instrument | CCDs | FITS prefix | Data dir |
|---|---|---|---|
| muscat  | 3 | `MSCT`   | `/data/MuSCAT`   |
| muscat2 | 4 | `MCT2`   | `/data/MuSCAT2`  |
| muscat3 | 4 | `ogg2m001-` | `/data/MuSCAT3` |
| muscat4 | 4 | `coj2m002-` | `/data/MuSCAT4` |
| sinistro | 1 | `*` (any LCO 1m site) | `/data/Sinistro`  |
| sbig | 1 | `*` (any LCO 0.4m site) | `/data/SBIGSTL6303` |
| qhy600 | 1 | `*` (any LCO 0.4m site) | `/data/QHY600CMOS` |

Sinistro/sbig/qhy600 scan the reduced `*e91.fits` frames produced by LCO BANZAI, regardless of site prefix (`elp1m008-`, `coj1m003-`, `cpt1m013-`, `ogg0m406-`, `coj0m416-`, …). sbig (SBIG STL-6303, `INSTRUME` prefix `kb`) is archival-only -- LCO's live instrument API has no schedulable instrument_type for it. qhy600 (QHY600 CMOS on DeltaRho 350, `INSTRUME` prefix `sq`) is the current live 0.4m camera and is schedulable; both prefixes and the full header convention (WCS, `GAIN=1`, `CONFMODE`) are confirmed against real archived frames.

The exposure calculator uses these instrument references when scaling its
MuSCAT3 calibration. Full well is in electrons, gain in electrons/ADU, pixel
scale in arcsec/pixel, and aperture in metres.

| Instrument | Full well | Gain | Pixel scale | Aperture |
|---|---:|---:|---:|---:|
| muscat | 55,000 | 1.0 | 0.358 | 1.88 |
| muscat2 | 62,000 | 1.0 | 0.44 | 1.52 |
| muscat3 | 99,000 | 1.8 | 0.267 | 2.0 |
| muscat4 | 99,000 | 1.8 | 0.267 | 2.0 |
| sinistro | 100,000 | 1.5 | 0.39 | 1.0 |
| sbig | 102,400 | 1.0 | 0.58 | 0.4 |
| qhy600 | 47,400 | 1.0 | 0.74 | 0.35 |

## Installation and usage

Choose either workflow below. Commands elsewhere in this README use the
installed `muscat-db` executable; when using `uv`, prefix those commands with
`uv run`.

<details>
<summary><strong>uv (recommended for repository development)</strong></summary>

Install the locked project environment from the repository root:

```bash
uv sync
```

Run the CLI or start the web interface inside that environment:

```bash
uv run muscat-db --help
uv run muscat-db serve
```

</details>

<details>
<summary><strong>pip</strong></summary>

Create and activate a virtual environment, then install the project in editable
mode:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Run the installed CLI or start the web interface:

```bash
muscat-db --help
muscat-db serve
```

</details>

## Configuration

Runtime configuration is read from environment variables. Core paths and safe
operational settings have in-code defaults, while optional integrations (for
example live LCO access, ADS search, and nova astrometry) require credentials.
The central registry in `src/muscat_db/config.py` drives the startup status
report, and `.env.example` documents the common deployment settings.

On import muscat-db auto-loads a `.env` file (via `python-dotenv`,
`find_dotenv` searching upward from the working directory). A `.env` is
**optional**. When absent, `load_dotenv` is a no-op and the defaults apply.
Copy the template only when you want to override a default or pin a value:

```bash
cp .env.example .env   # then edit
```

`MUSCAT_DATA_DIR` is the common raw-data root, not one instrument's directory.
Each instrument resolves below it using its canonical case-sensitive directory
name, for example `$MUSCAT_DATA_DIR/MuSCAT3/<yymmdd>/`.

### HARPS/LAMOST catalog files

The `/toi` and `/nexsci` pages' "has HARPS RV" / "has LAMOST RV" filters
coordinate-match against two catalogs:

- `data/HARPS_RVBank_targets.csv` (or `data/HARPS_RVBank_ver02.csv[.zip]` as a
  fallback) — override the path with `MUSCAT_HARPS_TARGETS_CSV` /
  `MUSCAT_HARPS_RVBANK_CSV` / `MUSCAT_HARPS_RVBANK_ZIP`.
- `data/LAMA_stars.csv` — override with `MUSCAT_LAMOST_STARS_CSV`.

Neither is fetched by `scripts/download_catalogs.sh`, both are gitignored, and
there is no online fallback for this coordinate-match path (unlike the target
page's live HARPS lookup) — without them, `_harps_coord_membership`/
`_lamost_coord_membership` in `catalog.py` silently return zero matches (no
error), so the header counts read 0 and the filter chips return nothing. A
deploy checkout brought up via `git reset --hard` never carries gitignored
files, so it starts out missing these two.

Point the three env vars above at a checkout that already has the files
(e.g. the dev checkout) instead of copying them into every deploy checkout —
same reasoning as `MUSCAT_DB_PATH` pointing production at a shared DB in
[Configuration](#configuration). A copy works too, but silently drifts stale
with no re-sync mechanism, which is how this got missed in the first place.

## Cron (daily)

The production database is rebuilt once a day from a crontab entry on the
deployment host (not checked in, since it's host-specific — the account,
`MUSCATDB_ROOT`, and `MUSCAT_OBSLOG_DIR` all vary by host):

```
MUSCAT_OBSLOG_DIR=/ut2/muscat/obslog
MUSCATDB_ROOT=/path/to/muscat-db
30 17 * * * cd $MUSCATDB_ROOT && bash deploy/nightly.sh run
30 21 * * * cd $MUSCATDB_ROOT && bash deploy/nightly.sh check
```

`deploy/nightly.sh run` is the old `download_catalogs && scan-yesterday &&
build-db` chain, with the same per-step logs under `logs/`. The first failing
step stops the chain and posts its name, exit code and the last 15 log lines to
Slack, so a refused build (the integrity gate, #182) is no longer silent
(#237). The catalog download is the exception: it retries each catalog
(DNS failures included), keeps the previous file when a download fails or is
not a CSV, and exits non-zero naming the catalogs it kept, which `run` posts as
a warning before carrying on with the scan and build. `check` runs `muscat-db build-status --max-age-hours 36` and alerts if
the database was not rebuilt, which also catches a cron entry that never fired.
Both use the webhook file `/etc/muscat-db/slack-webhook-url` that
`deploy/pull-deploy.sh` uses (override with `SLACK_WEBHOOK_FILE`); without it
the failure is still written to stderr. `muscat-db build-status` can also be
run by hand: it prints the age of the last build and exits 1 when stale.

`MUSCAT_OBSLOG_DIR` is exported inline rather than left to `.env` because cron
runs with a minimal environment; pin it in `.env` too for manual/GUI runs (see
[Configuration](#configuration)). `scan-yesterday` only scans the previous
day's obslog, so a one-off manual `build-db` or `scan` never gets silently
overwritten by the next nightly run of this job.

A scan that fails for one instrument is logged at WARNING with its traceback
in `scan.log`, and `scan-yesterday` names it in its output. It is also
recorded in `$MUSCAT_OBSLOG_DIR/.scan-failures.jsonl`, one entry per
instrument and date, until a later scan of that date writes cleanly. The same
happens for a CCD whose CSV could not be written, which would otherwise look
complete to `scan-missing`. `muscat-db scan-failures` lists the open entries.
`scan-yesterday` still exits 0 on such a failure, so the other instruments'
`build-db` goes ahead.

## Cron (weekly): backfill sweep and ingestion audit

`scan-yesterday` looks at each date once, so a date that fails or arrives late
is never revisited, and nothing used to notice. Two weekly jobs close that gap
(#196). Run them at low priority, well clear of the 17:30 nightly:

```
0 3 * * 0 cd $MUSCATDB_ROOT && nice -n 19 ionice -c3 uv run muscat-db sweep --build-db >> $MUSCATDB_ROOT/logs/sweep.log 2>&1
0 6 * * 0 cd $MUSCATDB_ROOT && nice -n 19 ionice -c3 uv run muscat-db audit >> $MUSCATDB_ROOT/logs/audit.log 2>&1
```

`sweep` rescans every date `scan-missing <inst> all` would pick up, for every
instrument, then retries the open scan failures. With `--build-db` it rebuilds
the database afterwards, but only if it rewrote any obslog CSVs. It stays out
of the way of photometry: it does not start while any photometry/fit job is
active, uses 8 workers by default (`--workers`), and a second sweep never runs
alongside the first. It holds a date, rather than rescanning it, when:

- the date has a known cause in `audit` (the #197 and #198 dates), or
- any CCD has fewer raw files than its existing CSV has rows, so a rescan
  would replace good rows with fewer or none.

It also skips incomplete dates whose raw directory has not changed since the
sweep last rescanned them (recorded in
`$MUSCAT_OBSLOG_DIR/.sweep-state.json`). Some dates hold FITS files whose
headers cannot be read, which keeps them "incomplete" however often they are
rescanned; without this, every weekly run would redo them for nothing.

`audit` compares, per instrument, date and CCD, the raw files `scan_date`
would match with the rows in `frames`, and reports every difference as
`missing` (an ingestion gap), `extra` (a rescan would lose rows) or `gone`
(rows for a date whose raw directory no longer exists). It only reads. Dates
younger than 3 days are skipped (`--min-age-days`), since their delivery may
still be arriving. The full report goes to `audit.log`. Slack (the same
`/etc/muscat-db/slack-webhook-url` as `deploy/pull-deploy.sh`, or
`$SLACK_WEBHOOK_FILE`) gets only what is new or changed since the previous
run, recorded in `$MUSCAT_OBSLOG_DIR/.audit-last.json`, plus any new open
scan failures. Dates with a known cause are tagged with their issue and never
alert; drop them from `audit._KNOWN_ISSUES` when the issue is fixed. Use
`--no-notify` for a manual run that should neither post nor move that
baseline.

`build-db` snapshots the existing database before rebuilding it (SQLite backup
API, safe while the server is running) to `$MUSCAT_DB_BACKUP_DIR` (default
`$MUSCAT_TMPDIR`, i.e. `~/temp`) as `muscat.db.nightly-<stamp>.sqlite`, keeping
the newest `$MUSCAT_DB_BACKUP_KEEP` (default 2). It aborts, leaving the live
file untouched, if either that snapshot or the freshly built database fails
`PRAGMA integrity_check`; a failed snapshot is kept with an `.INTEGRITY-FAILED`
suffix. `integrity_check` output that is only `Page N: never used` unreferenced
pages: the build goes on, but a Slack alert is raised because the pages can also
hide lost rows. Such a snapshot is marked, the newest clean snapshot is never
pruned, and a leaked copy with fewer rows in an app-owned table than that clean
one is refused as damage. The integrity checks
add roughly 3-4 minutes to the nightly run on an idle disk; see
[Database integrity failure](notes/DEPLOYMENT.md#database-integrity-failure) when
it refuses or runs for hours.

The rebuild ingests and validates a separate catalog, then publishes `frames`,
`summaries`, `targets`, and `db_meta` in one SQLite transaction. Application
state (including notes, users, access grants, jobs, leases, and reservations)
remains in the live database, so commits made during the rebuild survive.
Target keys are refreshed against the current grants in that same transaction.
WAL readers retain a consistent snapshot; writers wait during publication and
may time out if it exceeds their busy timeout (normally 30 seconds). The log
reports the publication transaction duration. A failed publication rolls back
the catalog changes. Accepted leaked pages are reclaimed with a live `VACUUM`
before publication, which also takes the write lock; healthy databases are not
vacuumed automatically.

### LCO proposal sync

`muscat-db lco-sync <PROPID>...` syncs a proposal's BANZAI final products
(RLEVEL 91, `OBSTYPE=EXPOSE`, engineering frames such as auto-focus dropped)
from the LCO archive into the download root (`MUSCAT_LCO_DIR`, else
`MUSCAT_DATA_DIR`). It catches observations the UI's request monitor never
saw, e.g. ones scheduled directly on the LCO portal. To run it in the same
nightly job, put it ahead of `build-db` and join it with `;` so an archive
outage cannot block the rebuild:

```
30 17 * * * cd $MUSCATDB_ROOT && uv run muscat-db lco-sync KEY2026B-001 --user <name> --no-ingest >> $MUSCATDB_ROOT/logs/lco-sync.log 2>&1; bash deploy/nightly.sh run
```

- **Whole nights.** The unit of work is a dataset: one instrument night (the
  `<Instrument>/<DAY-OBS>` directory). A night is selected when any of its
  frames has a DATE_OBS in the window, and it is then synced whole, even when
  `--start` or `--end` cuts through it. The archive is queried two days beyond
  each edge, one day at a time, so a cut night comes back complete. Because
  the window is DATE_OBS in UTC, a night can be labelled with the day before
  `--start`.
- **One night at a time.** Each night's missing frames are downloaded and
  funpacked, and the night is rescanned (and ingested) before the next night
  starts. An interrupted run therefore leaves at most one partial night.
- **Resuming.** Nothing is recorded between runs. Each run compares every
  night's archive listing with the unpacked files on disk, prints
  `N frames, M missing` per night, and fetches only the missing frames. A
  `.fz` whose funpack never finished is unpacked. `.part` files over an hour
  old, left by a killed run, are removed. A night whose obslog is older than
  its newest frame is rescanned. Ctrl-C stops after the in-flight downloads,
  each of which is written atomically. After any interruption, re-run the
  same command.
- **Window.** `--days 7` (default) re-checks a week on every run. Nights that
  are already complete cost only metadata queries, and the overlap is what
  picks up reductions BANZAI publishes days late. Use `--start/--end` (ISO
  UTC) for a one-off backfill.
- **Token.** `--user <name>` uses that muscat-db user's LCO token saved in
  Settings. Without it the command falls back to `$LCO_API_TOKEN`. The token
  must belong to a member of the proposal, since unreleased frames are
  proprietary.
- **Volume.** A busy 0.4m key project can produce thousands of QHY600 frames a
  night (about 67 MB each once the `.fz` and the unpacked `.fits` are both on
  disk). Run `--dry-run` first to see the per-night counts. `--max-nights N`
  downloads at most N incomplete nights per run, oldest first. The rest are
  reported as deferred and picked up by later runs.
- **Ingest.** `--no-ingest` stops after writing the obslog CSVs and leaves
  ingestion to the `build-db` that follows. Without that flag, each night is
  also ingested as soon as it is scanned.
- **Safety.** A lock file (`.lco-sync.lock` in the download root) makes an
  overlapping run exit instead of racing the first one. The lock is released
  when the process dies, so a killed run never blocks the next. The exit
  status is non-zero if any frame, scan, or proposal failed.

## Documentation

The published docs site is https://muscat-team.github.io/muscatdb/ (updates when
`main` is released). The UI tour is https://muscat-team.github.io/muscatdb/home/.

On a running instance, the operator Guide is `/guide`. FastAPI Swagger is `/docs`.

To preview the tour locally after building docs:

```bash
uv run mkdocs build --site-dir site
uv run muscat-db build-static-site --out site/home --db mock_muscat.db --no-figures
```

Use a real `muscat.db` on the host instead of the mock database when you want
real figures. Rebuild the snapshot after MkDocs; `mkdocs build` deletes `site/`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, the pre-PR checklist,
and the branch/PR workflow.

## Known limitation: the UTC-midnight dataset split

`obsdate` is **not** the observing night — it is the UTC calendar date, inherited
from the directory the frames sit in. A single continuous night whose frames
straddle 00:00 UTC is therefore recorded as two datasets, and appears as two rows
on the target page.

Verified example (`TIC 460950389.01 / TOI 6715.01`, Cerro Tololo, UTC−4):

```
240417  sinistro  i  1.26–1.26    1 frame    UT 23:59:57
240418  sinistro  i  1.22–1.37  174 frames   UT 00:01:28 – 04:19:28
```

Local time 19:59 → 00:19: one continuous ~4.3 h run, cut by a UTC boundary that
has nothing to do with the night.

### Where the date comes from

The LCO archive already stamps DAY-OBS into every filename, and it is correct:
all 175 frames above are named `lsc1m004-fa03-20240417-NNNN-e91`, sequence
0092→0267 contiguous. `lco.py` `frame_destination()` ignores that token and
derives the directory from `DATE_OBS` (the actual UTC timestamp) instead, so the
post-midnight frames land in `240418`. `scanner.py` `_find_fits_files()` then
globs `<data_dir>/<obsdate>/`, and the directory name becomes the DB grouping key
verbatim.

**The correct directory for any LCO frame is therefore readable from its own
filename** — no timezone or longitude arithmetic is required to detect or repair
a split.

### Which instruments are affected

LCO-fed instruments only: sinistro, muscat3, muscat4. **muscat and muscat2 are
not affected.** MuSCAT2's telescope naming is already observing-night based —
1,079 of its 1,247 date directories contain post-midnight UT frames (e.g. dir
`200113` runs through UT 00:59:45), so its nights are never cut.

Beware of surveying this with `MIN`/`MAX` on `ut_start`: it is a time-of-day
**string**, so `MAX(ut_start)` returns the largest clock reading (23:5x) rather
than the chronologically last frame. That flags every pair of consecutive nights
that each span midnight and produces large false-positive counts. Compare
`jd_start` instead, or compare each file's DAY-OBS token to its directory:

```sql
-- authoritative: a file whose parent directory disagrees with its own DAY-OBS
SELECT instrument, obsdate, substr(filename, 15, 8) AS dayobs, COUNT(*)
FROM frames WHERE instrument IN ('sinistro','muscat3','muscat4')
GROUP BY instrument, obsdate, dayobs;
-- a directory yielding >1 distinct dayobs is a split
```

As of the 2026-07 survey, that query gives three distinct populations:

| Population | Count | Meaning |
|---|---:|---|
| Mixed dirs, adjacent day | 16 dirs | genuine midnight splits (12 sinistro, 3 muscat3, 1 muscat4) |
| Mixed dirs, non-adjacent | 4 dirs | stray/misfiled data, unrelated to midnight (e.g. `sinistro/220309` holds 181 frames dated `221209`) |
| Pure but offset | 59 dirs, 42,674 files | whole directory labelled with the UTC date rather than DAY-OBS; internally consistent, **not** split |

Only the first population causes the two-row symptom. See
[Repairing a split](#repairing-a-split-disk-consolidation) below.

### The night convention already exists elsewhere

Two other parts of the stack already use an observation-night date, which is why
this reads as an internal inconsistency rather than a missing feature:

- `lco.py` `_lco_observing_date()` does site-timezone conversion plus
  roll-back-before-local-noon — but it only feeds scheduler dataset matching,
  never the download destination.
- The photometry pipeline embeds an obs-night date in output filenames.
  `photometry.py` `discovered_targets()` and `list_outputs()` both document that
  this "may differ from the directory name (obs-night vs UT date)" and build
  regexes accepting any 6-digit date token to paper over the mismatch.

### Two constraints shaped the fix

**1. Use the filename's DAY-OBS token, not local-evening date.**
`_lco_observing_date()` is the obvious function to reach for and is the wrong
one: it returns the local *evening* date, which for western sites differs from
the current directory name for the **whole** night, not just the post-midnight
part. Adopting it would relabel every elp (26,333 frames) and ogg (115) sinistro
dataset — plus all of muscat3 — by one day.

The correct convention is the **UTC date at the start of the observing night**,
which is exactly what LCO already writes into each filename. Reading the token
merges the split nights, leaves every currently-unsplit label byte-identical, and
requires no site table.

**2. A DB-layer night key alone does not make the merged night reducible.**
`photometry.py` passes a single `--data_dir` to `run_photometry`, and prose
derives both the instrument and the obslog path from that directory's name. A
night spanning two `obsdate` directories cannot be reduced in one run by
regrouping in SQL alone — that fixes the browsing view while leaving photometry
unable to act on it. A fix has to either consolidate the frames on disk or teach
the pipeline to accept multiple input directories; the tool below takes the
disk-consolidation path.

### Repairing a split (disk consolidation)

`build-db` rebuilds `frames`, `summaries`, and `targets` entirely from the
obslog CSV tree and publishes them atomically. App-owned tables (notes,
overrides, jobs, saved ephemeris views) are preserved; the observation-derived
ones are not. **An edit made directly to an observation-derived table is erased
by the next nightly cron run.**

Publication copies rows and maintains indexes in batches of 10,000, allowing
application writes between commits. A short final transaction switches all
catalog tables and refreshes target keys together; readers see either catalog,
never a partial replacement. Retired tables are cleared in batches. Interrupted
copies are cleaned up by the next rebuild, and concurrent builders serialize
through a `.build.lock` file. Budget disk space for the snapshot, staging file,
old and new catalog tables, and WAL; old readers may retain WAL pages.

Online rebuilds do not run `VACUUM`: it holds SQLite's writer lock for the whole
database. If the integrity gate accepts only leaked-page warnings, the rebuild
retains them and logs `schedule VACUUM during maintenance`. Keep the clean backup
and follow the [integrity recovery procedure](notes/DEPLOYMENT.md#database-integrity-failure)
in a maintenance window to compact the database. Application writer timeouts
remain 30 seconds.


The source-of-truth chain, top to bottom:

```
raw FITS      $MUSCAT_DATA_DIR/<InstDir>/<yymmdd>/*.fits        <- consolidate HERE
   |  scan_date()  -> obslog-<inst>-<yymmdd>-ccd<N>.csv, opened mode "w"
obslog CSV    $MUSCAT_OBSLOG_DIR/<inst>/<yymmdd>/
   |  _discover_csv_jobs() -> build-db
muscat.db     frames / summaries / targets    (fully derived - never edit)
```

Rule: *a file belongs in the directory matching the 8-digit DAY-OBS token in its
own filename.* This is implemented by `muscat_db/obsdate_normalize.py` and driven
by:

```bash
muscat-db normalize-obsdates             # dry run over every LCO-fed instrument
muscat-db normalize-obsdates sinistro    # dry run, one instrument
muscat-db normalize-obsdates --apply     # perform it, then rescan + re-ingest
muscat-db normalize-obsdates --apply --no-rescan
```

It is a **dry run unless `--apply` is passed**, and it reports rather than moves
anything it cannot decide safely. Each run performs the moves, deletes the obslog
CSVs they invalidate, removes directories left empty, then rescans and re-ingests
every affected date. `lco.py` `frame_destination()` now derives the download
directory from the same filename token, so new downloads do not re-create splits.

Clearing the invalidated CSVs is the step that silently undoes everything if
skipped. `scan_date` writes CSVs only for CCDs that produced rows, and only
removes a stale one on its own in two narrower cases: a multi-CCD instrument
whose sibling CCD proves the date directory is genuinely readable this same
call (#108), or a single-CCD instrument (sinistro/sbig/qhy600) whose CSV has
gone unconfirmed for longer than `MUSCAT_SCAN_STALE_CSV_GRACE_S` (72h default,
#115) — and only when scanning the canonical `MUSCAT_DATA_DIR` root. Move
every frame of a CCD out of a directory and its old CSV can survive well past
that (indefinitely, if nothing ever rescans that exact date again),
`_discover_csv_jobs` still finds it, and `build-db` re-ingests the pre-move
split — the frames moved but the database looks untouched. `normalize-obsdates
--apply`'s own unconditional clearing (below) is the reliable, immediate path;
don't rely on either passive mechanism above to catch a consolidation.

The normalizer only considers files the instrument's own scanner would ingest,
reusing `scanner._find_fits_files` rather than re-deriving the glob. This matters:
the raw tree contains cross-filed data — `/data/MuSCAT3/231007` holds 1,900
`coj2m002-` (MuSCAT4) frames that no instrument scans — and relocating those would
churn files without changing anything the database sees, while still leaving them
in the wrong instrument tree.

What can erase a consolidation:

| Eraser | Mechanism | Prevention |
|---|---|---|
| `build-db` | drops and rebuilds from CSVs | consolidate in the FITS tree, never in `muscat.db` |
| stale obslog CSV | usually survives a rescan (removed only by #108's same-call multi-CCD proof, or a single-CCD instrument's 72h grace window, #115); still ingested until then | delete it, or rename its dir to a non-YYMMDD name |
| `scan-missing` / `scan-all` | regenerates any raw dir whose name is not an existing obslog dir | keep raw and obslog dir names consistent; remove emptied raw dirs |
| `scan-yesterday` (cron) | scans yesterday only | historical consolidation is never revisited — this is why a one-off move survives |
| LCO re-download | `frame_destination()` uses `DATE_OBS` | fix it, or every consolidation is temporary |

Escape hatch: `_discover_csv_jobs` skips directories failing `_is_obsdate()`, so
renaming `240418` to `240418_presplit` keeps it on disk for provenance while
excluding it from `build-db`. This is an existing intentional pattern — the code
comment names `csv_old_220914` and `Hyades` as precedents.

Because the rule is filename-driven the repair is idempotent, so it can be
re-asserted rather than performed once. Running `normalize-obsdates --apply`
before `scan-yesterday` in cron corrects any split reintroduced by a re-download
or a manual copy on the next pass.

### What the normalizer refuses to do

Three classes are reported and left alone, because each needs a decision the tool
cannot make:

| Reason | Meaning | Why it is not automated |
|---|---|---|
| `offset` | every frame in the directory carries the same DAY-OBS, and no directory of that name exists | moving them is a bare directory rename, changing a dataset label that photometry output paths, `target_notes` primary keys `(object, obsdate, instrument)` and job records already reference |
| `stray` | a frame whose DAY-OBS is more than a day from its directory | misfiled data, not a midnight artifact — e.g. `sinistro/220309` holds frames dated `221209`, nine months off |
| `conflict` | the destination already holds a file of that name | the same frame exists in both directories; deleting a copy is a data-removal decision, so the tool never does it implicitly |

Conflicts are **duplicate copies of the same frame**, not competing reductions.
They are not a `.fits` / `.fits.fz` pairing either: the scanner glob is
`*e91.fits`, so a conflict only fires when the identical unpacked basename exists
in both directories.

A 450-pair sample (150 per instrument) found every pair carrying the same
`DATASUM`, `PIPEVER`, `DATE-OBS`, `DAY-OBS`, `OBJECT`, `EXPTIME` and `ORIGNAME` —
the pixel data is byte-identical. The files nonetheless differ under `md5sum`
because the FITS `CHECKSUM` card is recomputed on write; in the sampled pairs the
difference was confined to that single card, or the files were byte-identical
outright. Comparing file size or `md5sum` is therefore misleading here — compare
`DATASUM`:

```bash
python -c "
from astropy.io import fits
a,b='<dir-A>/<frame>.fits','<dir-B>/<frame>.fits'
print(fits.getheader(a).get('DATASUM'), fits.getheader(b).get('DATASUM'))"
```

Because the copies are redundant, the resolution is to delete the one in the
wrong directory and keep the one already under its DAY-OBS. That is left manual:
the normalizer relocates frames but never deletes science data.

A directory holding more than one DAY-OBS is always repaired: two nights sharing
one directory is unambiguously wrong, so the minority moves even when the
destination has to be created. A directory whose frames all agree with each other
is only repaired when that night already has a home elsewhere.

As of the 2026-07 dry run: **10,328 frames** would move (sinistro 7,427 across 17
directory pairs, muscat3 2,861 across 3, muscat4 40 across 1), with 28 `offset`,
6 `stray` and 7,083 `conflict` reports left for review.

### Target LCO archive inventory

Open **LCO archive** on a target page to find reduced science observations
covering its coordinates, including nights never reported to ExoFOP. The panel
loads only when opened. Each row groups a night, instrument and LCO request,
with filters, exposure times, proposal, frame count and proprietary status.
Local presence uses the individual frame filenames in the database; a partial
request remains downloadable. **Download** queues the existing whole-request
download and ingest workflow. Follow progress in **Jobs**, then search again
or reopen the panel to see the **Photometry** link.

Archive metadata is cached for one hour per coordinates, user, token and date
range; local presence and proposal access are checked on each load. A viewer
with restricted proposals needs their own LCO token in Settings. The inventory
can only show frames visible to that token and permitted by local access rules.
A proprietary label means the archive's public-release date is still in the
future; it does not prevent an authorized download.

Searches follow [LCO cursor pagination](https://developers.lco.global/#pagination)
in sequential pages of 1,000 frames, stopping after 50 pages or 50,000 frames.
A search that reaches this budget is labeled incomplete; narrow the dates to
retrieve the remaining nights. Incomplete inventories are not cached. Progress
streams while the search runs. An error
on any page leaves the search incomplete and uncached; use Search to retry.
Optional UT date filters (inclusive end date) narrow the search, and persist per
target in the browser. ExoFOP-reported nights are included: a report does not
account for every request, filter or telescope observed that night. ExoFOP indicators match instrument,
site and night within one day, not individual request IDs. “No matching report”
reflects the existing ExoFOP lookup, which may also return no rows during an outage.


### Complete searches on the LCO archive page

On `/lco/archive`, **All matching frames** is the default search scope. Object
searches follow cursor pages, including nights beyond the former 10,000-frame
limit, with a server ceiling of 50 pages or 50,000 frames. Budget-limited results
are labeled incomplete; narrow the target, proposal or dates before retrying.
Choose **Quick preview** to deliberately cap retrieval (up to 10,000 frames);
truncated previews are labeled incomplete. A limit saved by an older version does
not enable preview mode: the new scope must be selected explicitly.

Results render 200 frames per display page. Selections survive Previous/Next,
and **Download selected** includes selected frames from every display page. Group
checkboxes select only the displayed frames. Search settings persist in the
browser; large result sets remain in memory only for the current visit and must
be searched again after returning. Small results still restore as before. Whole
request downloads retain their existing safety limits.
