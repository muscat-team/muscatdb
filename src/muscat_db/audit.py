"""Raw-vs-db ingestion audit (issue #196, part 3).

The ingestion gaps in #157 (~5.6M raw frames never in ``muscat.db``) were
found by hand, years after they opened. This compares, for every instrument,
date and CCD, the raw files ``scan_date`` would pick up against the ``frames``
rows built from them, and reports every difference.

It uses ``scanner._find_fits_files`` itself rather than counting files: a bare
count produced false mismatches in #157, since e.g. muscat3 only matches
``*e91.fits`` under its current epoch names.

The audit only reads. Each mismatch gets a ``kind``:

* ``missing`` -- raw files with no rows: an ingestion gap. ``sweep`` (or
  ``scan`` + ``build-db``) fixes it.
* ``extra`` -- more rows than raw files on some CCD. A rescan would replace
  good rows with fewer or none, so this needs a person, not a rescan (#197).
* ``gone`` -- rows for a date whose raw directory no longer exists (#198).

Dates already explained by an open issue are tagged with it (``known``) and
never alert, but stay in the report so they cannot be forgotten.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import sqlite3
import urllib.request
from dataclasses import dataclass

from muscat_db import scan_failures
from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE
from muscat_db.scanner import _find_fits_files, _is_obsdate_dir

logger = logging.getLogger(__name__)

# Archive delivery for a night can trail it by up to ~62h (see
# scanner._DEFAULT_STALE_CSV_GRACE_S); younger dates are not yet expected to
# match and would only add noise.
DEFAULT_MIN_AGE_DAYS = 3
STATE_NAME = ".audit-last.json"
_SLACK_DEFAULT_WEBHOOK_FILE = "/etc/muscat-db/slack-webhook-url"
_SLACK_MAX_LINES = 20

# Dates where raw and db legitimately disagree until a person resolves the
# linked issue. A rescan of any of them would destroy good rows, so `sweep`
# holds them too. Remove an entry when its issue is fixed.
#
# The #213 entry is the 2026-10-08 duplicate cleanup. The duplicate rows were
# removed from the db and their obslog CSVs quarantined, but the raw copies
# are still on disk under the wrong-date folder, so rescanning any of them
# would re-ingest rows the real night already holds. The shrink guard cannot
# see this: it only compares against existing CSV rows, and these dates have
# none left. The hold itself is the protection; drop it only when the copies
# are gone or a person has decided what to do with them.
_KNOWN_ISSUES: dict[str, tuple[tuple[str, str], ...]] = {
    "#197": tuple(("muscat3", d) for d in (
        "210110", "210127", "210210",  # old ep01-ep04 epoch names
        "231111",                      # every row duplicated
        "210408",                      # unreduced e00 frames ingested
    )),
    "#198": (
        ("muscat3", "220309"), ("muscat3", "251111"),  # date directory gone
        ("muscat3", "250722"),                         # only .fits.fz on disk
    ),
    "#213": (
        ("muscat3", "260729"), ("muscat3", "260727"), ("muscat3", "250704"),
        ("muscat3", "260716"), ("muscat3", "260723"),
        ("sinistro", "260722"),
    ),
}


def known_issues() -> dict[tuple[str, str], str]:
    """``{(instrument, obsdate): issue}`` for every date with a known cause."""
    return {key: ref for ref, keys in _KNOWN_ISSUES.items() for key in keys}


@dataclass(frozen=True)
class Mismatch:
    instrument: str
    obsdate: str
    raw: tuple[int, ...]
    db: tuple[int, ...]
    kind: str
    known: str | None = None

    @property
    def key(self) -> str:
        return f"{self.instrument} {self.obsdate}"

    def line(self) -> str:
        tag = f"  (known: {self.known})" if self.known else ""
        return (
            f"{self.key}  {self.kind:<7}  raw {list(self.raw)}  db {list(self.db)}{tag}"
        )


def _db_counts(db_path: str, instruments: list[str]) -> dict[tuple[str, str, int], int]:
    marks = ",".join("?" * len(instruments))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute(
            f"SELECT instrument, obsdate, ccd, COUNT(*) FROM frames "
            f"WHERE instrument IN ({marks}) GROUP BY instrument, obsdate, ccd",
            instruments,
        ).fetchall()
    finally:
        conn.close()
    return {(i, d, c): n for i, d, c, n in rows}


def _raw_dates(inst_name: str) -> set[str]:
    data_dir = INSTRUMENTS[inst_name].data_dir
    try:
        entries = os.listdir(data_dir)
    except FileNotFoundError:
        return set()
    return {d for d in entries if _is_obsdate_dir(d) and os.path.isdir(os.path.join(data_dir, d))}


def _settled(obsdate: str, cutoff: datetime.date) -> bool:
    try:
        return datetime.datetime.strptime(obsdate, "%y%m%d").date() <= cutoff
    except ValueError:
        return False  # a non-canonical label in frames; not a date to audit


def run_audit(
    db_path: str,
    today: datetime.date | None = None,
    instruments: list[str] | None = None,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> list[Mismatch]:
    """Every (instrument, date) whose per-CCD raw and db counts differ."""
    names = instruments or list(INSTRUMENTS)
    cutoff = (today or datetime.date.today()) - datetime.timedelta(days=min_age_days)
    counts = _db_counts(db_path, names)
    known = known_issues()
    found: list[Mismatch] = []
    for name in names:
        inst = INSTRUMENTS[name]
        raw_dates = _raw_dates(name)
        db_dates = {d for (i, d, _) in counts if i == name}
        for obsdate in sorted(raw_dates | db_dates):
            if not _settled(obsdate, cutoff):
                continue
            ccds = range(inst.nccd)
            on_disk = obsdate in raw_dates
            raw = tuple(len(_find_fits_files(inst, obsdate, c)) if on_disk else 0 for c in ccds)
            db = tuple(counts.get((name, obsdate, c), 0) for c in ccds)
            if raw == db:
                continue
            if not on_disk:
                kind = "gone"
            elif any(d > r for r, d in zip(raw, db)):
                kind = "extra"
            else:
                kind = "missing"
            found.append(Mismatch(name, obsdate, raw, db, kind, known.get((name, obsdate))))
    return found


# -- reporting ----------------------------------------------------------------


def _state_path() -> str:
    return os.path.join(OBSLOG_BASE, STATE_NAME)


def _load_state() -> dict[str, object]:
    try:
        with open(_state_path()) as f:
            return json.load(f).get("mismatches", {})
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable audit state %s: %s", _state_path(), exc)
        return {}


def _save_state(snapshot: dict[str, object], today: datetime.date) -> None:
    path = _state_path()
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump({"date": today.isoformat(), "mismatches": snapshot}, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not save audit state %s: %s", path, exc)


def post_slack(text: str) -> bool:
    """Post *text* to the deploy Slack webhook; False (and a warning) if unsent.

    Same webhook file as ``deploy/pull-deploy.sh``. Its URL is a secret, so it
    is never logged.
    """
    path = os.environ.get("SLACK_WEBHOOK_FILE", _SLACK_DEFAULT_WEBHOOK_FILE)
    try:
        with open(path) as f:
            webhook = f.read().strip()
    except OSError as exc:
        logger.warning("audit alert not sent: cannot read Slack webhook file %s: %s", path, exc)
        return False
    if not webhook:
        logger.warning("audit alert not sent: Slack webhook file %s is empty", path)
        return False
    req = urllib.request.Request(
        webhook, data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except OSError as exc:
        logger.warning("audit alert not sent: Slack POST failed: %s", exc)
        return False


def _alert_text(new: list[str], total: int, today: datetime.date) -> str:
    head = (
        f":mag: muscat-db ingestion audit on {socket.gethostname()}, {today.isoformat()}: "
        f"{len(new)} new or changed (of {total} open)"
    )
    shown = new[:_SLACK_MAX_LINES]
    more = len(new) - len(shown)
    tail = [f"... and {more} more"] if more else []
    return "\n".join([head, "```", *shown, *tail, "```", "Full report: logs/audit.log"])


def report(
    mismatches: list[Mismatch], today: datetime.date | None = None, notify: bool = True,
) -> list[str]:
    """Print the full report; alert Slack on what is new since the last run.

    Returns the new/changed lines. "New" compares with the snapshot saved by
    the previous notifying run, which is saved even when Slack is unreachable,
    so a failed post never turns into the same alert every week. With
    ``notify=False`` nothing is posted or saved.
    """
    today = today or datetime.date.today()
    failures = scan_failures.pending(OBSLOG_BASE)

    print(f"{len(mismatches)} raw/db mismatch(es), {len(failures)} open scan failure(s)")
    for m in mismatches:
        print(f"  {m.line()}")
    for e in failures:
        print(
            f"  {e['instrument']} {e['obsdate']}  scan failed x{e.get('attempts', 1)}"
            f" since {e.get('first_failed', '?')}: {e.get('reason', '')}"
        )

    snapshot: dict[str, object] = {m.key: [list(m.raw), list(m.db), m.kind] for m in mismatches}
    snapshot.update({
        f"scan-failure {e['instrument']} {e['obsdate']}": e.get("reason", "") for e in failures
    })
    lines = {m.key: m.line() for m in mismatches if not m.known}
    lines.update({
        k: f"{k}: {v}" for k, v in snapshot.items() if k.startswith("scan-failure ")
    })
    previous = _load_state()
    new = [lines[k] for k in sorted(lines) if previous.get(k) != snapshot[k]]
    if not notify:
        return new
    if new:
        post_slack(_alert_text(new, len(snapshot), today))
    _save_state(snapshot, today)
    return new
