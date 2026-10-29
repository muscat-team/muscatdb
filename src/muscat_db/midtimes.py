"""Parse an observer's list of predicted mid-transit times (/lco/schedule, #350).

Predictions often come from outside muscat-db (a collaborator's TTV model, a
photodynamical fit, ExoFOP), so the schedule page accepts them as text: one
time per line, optionally with a 1-sigma uncertainty.

This is a system boundary. A malformed line is reported by number, never
skipped, and a number that looks like a reduced BJD (BTJD, BKJD, ...) is
refused rather than scheduled thousands of days off.

Columns, by header name in any order, or positionally as ``tc, tc_err``
when the first line is not a header:

- ``tc``: BJD_TDB (full, > 2400000) or an ISO 8601 UTC time. Required.
- ``tc_err``: 1-sigma uncertainty in days, the same unit as numeric ``tc``.
"""

from __future__ import annotations

import datetime
import math
import re
from dataclasses import dataclass

# Bounds the visibility work one request can trigger (each row is a full
# per-site observability computation).
MAX_ROWS = 1000
MAX_TEXT_CHARS = 256_000
MAX_REPORTED_ERRORS = 20

# A full BJD_TDB is ~2.4-2.5 million; anything below this is a reduced system.
_MIN_FULL_BJD = 2_400_000.0
_UNIX_EPOCH_JD = 2440587.5
_MIN_PER_DAY = 1440.0

_HEADER_ALIASES = {
    "tc": "tc", "tmid": "tc", "mid": "tc", "midtime": "tc", "bjd": "tc", "bjd_tdb": "tc",
    "tc_err": "tc_err", "tc_unc": "tc_err", "err": "tc_err", "unc": "tc_err",
}
_POSITIONAL = ("tc", "tc_err")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


class MidtimeError(ValueError):
    """The uploaded list is unusable; ``errors`` names each offending line."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class Midtime:
    line: int
    bjd: float | None      # BJD_TDB when given as a number
    utc_jd: float | None   # JD_UTC when given as an ISO time
    unc_min: float | None


@dataclass(frozen=True)
class MidtimeList:
    rows: tuple[Midtime, ...]
    duplicates_dropped: int
    time_systems: dict[str, int]


def _split(line: str) -> list[str]:
    if "," in line:
        return [f.strip() for f in line.split(",")]
    if "\t" in line:
        return [f.strip() for f in line.split("\t")]
    return line.split()


def _parse_time(text: str) -> tuple[float | None, float | None]:
    """(bjd, utc_jd) from one ``tc`` field; raises ValueError with the reason."""
    if _ISO_RE.match(text):
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return None, dt.timestamp() / 86400.0 + _UNIX_EPOCH_JD
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"tc {text!r} is not a finite number")
    if value < _MIN_FULL_BJD:
        raise ValueError(
            f"tc {text!r} looks like a reduced BJD (e.g. BTJD = BJD - 2457000); "
            "give the full BJD_TDB or an ISO UTC time"
        )
    return value, None


def _parse_unc(text: str, unit_min: float) -> float | None:
    if not text:
        return None
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"uncertainty {text!r} must be a non-negative number")
    return value * unit_min


def _header_columns(fields: list[str]) -> list[str] | None:
    """Column names when ``fields`` is a header line, else None."""
    try:
        _parse_time(fields[0])
        return None
    except ValueError:
        pass
    names = [f.lower() for f in fields]
    if not any(n in _HEADER_ALIASES for n in names):
        return None  # a bad data line, not a header
    unknown = [f for f, n in zip(fields, names) if n not in _HEADER_ALIASES]
    if unknown:
        raise MidtimeError([f"line 1: unknown column {unknown[0]!r}"])
    columns = [_HEADER_ALIASES[n] for n in names]
    if "tc" not in columns:
        raise MidtimeError(["line 1: header has no 'tc' column"])
    return columns


def _parse_row(lineno: int, fields: list[str], columns: list[str] | tuple[str, ...]) -> Midtime:
    if len(fields) > len(columns):
        raise ValueError(f"{len(fields)} columns, expected at most {len(columns)}")
    values = dict(zip(columns, fields))
    bjd, utc_jd = _parse_time(values.get("tc", ""))
    unc = _parse_unc(values.get("tc_err", ""), _MIN_PER_DAY)
    return Midtime(
        line=lineno, bjd=bjd, utc_jd=utc_jd,
        unc_min=unc,
    )


def _data_lines(text: str) -> list[tuple[int, list[str]]]:
    out = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append((lineno, _split(line)))
    return out


def _dedupe(rows: list[Midtime]) -> tuple[list[Midtime], int, list[str]]:
    """Drop repeated times."""
    seen_times: set[float] = set()
    kept, errors = [], []
    for r in rows:
        key = r.bjd if r.bjd is not None else ("utc", r.utc_jd)
        if key in seen_times:
            continue
        seen_times.add(key)
        kept.append(r)
    return kept, len(rows) - len(kept) - len(errors), errors


def _truncate(errors: list[str]) -> list[str]:
    if len(errors) <= MAX_REPORTED_ERRORS:
        return errors
    return errors[:MAX_REPORTED_ERRORS] + [f"... and {len(errors) - MAX_REPORTED_ERRORS} more"]


def parse_midtimes(text: str) -> MidtimeList:
    """Parse ``text`` into mid-times; raises MidtimeError naming every bad line."""
    text = text or ""
    if len(text) > MAX_TEXT_CHARS:
        raise MidtimeError([f"upload too large ({len(text)} characters, at most {MAX_TEXT_CHARS})"])
    lines = _data_lines(text)
    if not lines:
        raise MidtimeError(["no mid-transit times found"])

    columns: list[str] | tuple[str, ...] = _POSITIONAL
    header = _header_columns(lines[0][1])
    if header is not None:
        columns = header
        lines = lines[1:]
    if len(lines) > MAX_ROWS:
        raise MidtimeError([f"{len(lines)} times given, at most {MAX_ROWS} per upload"])

    rows, errors = [], []
    for lineno, fields in lines:
        try:
            rows.append(_parse_row(lineno, fields, columns))
        except ValueError as e:
            errors.append(f"line {lineno}: {e}")

    rows, dropped, dup_errors = _dedupe(rows)
    errors.extend(dup_errors)
    if errors:
        raise MidtimeError(_truncate(errors))
    if not rows:
        raise MidtimeError(["no mid-transit times found"])

    n_bjd = sum(1 for r in rows if r.bjd is not None)
    return MidtimeList(
        rows=tuple(rows),
        duplicates_dropped=dropped,
        time_systems={"bjd_tdb": n_bjd, "utc": len(rows) - n_bjd},
    )
