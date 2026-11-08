"""Light curves uploaded from pipelines muscat-db does not run (issue #382).

A collaborator's table has arbitrary columns, and only the uploader knows which
is time, flux, error, and which should enter the detrending design matrix. The
uploader assigns every role by picking columns, so a file's own column names are
never interpreted. Saving writes:

* a normalized CSV with the columns the transit fit reads (``BJD_TDB``,
  ``Flux``, ``Err``, then the chosen covariates, under their original names),
  named ``<target>_external_<band>_<date>_<run_id>.csv`` so the transit fit's
  existing discovery and band parsing find it unchanged;
* a ``_product.json`` record (level ``photometry``) whose single ``upload``
  input names the original files and their SHA-256 checksums, and whose
  settings carry the column mapping.

Both go in ``<prose output>/external/<date>/_runs/<target>/upload-<sha8>/``,
where :data:`~muscat_db.instruments.EXTERNAL_INSTRUMENT` stands in for the
instrument.

Decisions on #382, all of which refuse rather than guess:

* time: the uploader declares the time system, and only BJD_TDB is accepted.
  JD_UTC needs the observatory's location to convert, which muscat-db does not
  know for another facility's telescope. Times below JD 2,400,000 are refused
  as an offset time (BTJD, BJD - 2457000, ...).
* a second file with covariates joins on one key column from each file, by
  exact value. A duplicated key in either file, or a row of either file with no
  match in the other, refuses the upload.
* a mapped covariate with no numeric value in any row is refused: timer's
  ``bin_df`` ends with ``dropna()``, so such a column would delete every row. A
  covariate missing only in some rows is kept with a warning, since timer drops
  those rows.
* the same primary file (same checksum) cannot be uploaded twice; the refusal
  names the product that already holds it.
* visibility follows the target's access rules; uploaders retain access to
  their own products (:mod:`muscat_db.product_visibility`).
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import math
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from muscat_db import __version__, jobs, product_record
from muscat_db.instruments import EXTERNAL_INSTRUMENT

PARSER_VERSION = 1

# One light curve is at most a few thousand rows; these bounds only stop a
# mistaken upload (a whole archive, a binary file) from tying up the server.
# A save posts both files base64-encoded (4/3 larger) in one JSON body, and
# nginx refuses bodies over 20 MB (``client_max_body_size`` in deploy/nginx*.conf)
# with an HTML 413, so two files at this limit must stay well under that.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 500
MAX_CELLS = 2_000_000
# Largest request body the upload routes read: two files, encoded, plus the
# mapping and form fields.
MAX_REQUEST_BYTES = 2 * ((MAX_UPLOAD_BYTES * 4) // 3 + 4) + 64 * 1024
MAX_FACILITY_LEN = 80
MAX_TARGET_LEN = 100
MAX_COMPONENT_BYTES = 255

TIME_SYSTEMS = ("BJD_TDB",)

# Bands the transit fit can map to limb-darkening coefficients, as the filename
# token it parses (transit_fit._normalize_band). Anything else would silently
# become 'g' there, so the uploader picks from this list.
BANDS = ("g", "r", "i", "z", "g_narrow", "r_narrow", "i_narrow", "z_narrow", "Na_D")

# Output columns the transit fit reserves for time, flux and error.
RESERVED_COLUMNS = ("BJD_TDB", "Flux", "Err")

# Any full Julian date in the CCD era is above this. A smaller value is an
# offset time (BTJD, BJD - 2457000, MJD), which cannot be told apart reliably.
_MIN_FULL_JD = 2_400_000.0
_UNIX_EPOCH_JD = 2440587.5
# How far the first timestamp may sit from the night the uploader names. Wide
# enough for any observatory's local evening, narrow enough to catch a wrong
# date or a wrong time system off by days.
_MAX_NIGHT_OFFSET_DAYS = 1.5

RUN_PREFIX = "upload-"
_CHECKSUM_PREFIX_LEN = 8

_MISSING_CELLS = frozenset({"", "nan", "-nan", "+nan", "none", "null", "na", "n/a"})
_DELIMITERS = ",\t;|"
_DATE_RE = re.compile(r"^\d{6}$")


class UploadError(ValueError):
    """A refused upload. The message is shown to the uploader as is."""


@dataclass(frozen=True)
class Table:
    """A parsed upload: column names and rows of raw cell text."""

    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Mapping:
    """Which uploaded column plays which role.

    ``join_primary`` and ``join_ancillary`` name the key column in each file
    when a second file supplies ``ancillary_covariates``.
    """

    time: str
    flux: str
    err: str
    time_system: str
    covariates: tuple[str, ...] = ()
    ancillary_covariates: tuple[str, ...] = ()
    join_primary: str = ""
    join_ancillary: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> "Mapping":
        """Build a mapping from a client's JSON, refusing a malformed one."""
        if not isinstance(payload, dict):
            raise UploadError("mapping must be an object")

        def name(key: str, required: bool = True) -> str:
            val = payload.get(key, "")
            if not isinstance(val, str):
                raise UploadError(f"mapping.{key} must be a column name")
            if required and not val.strip():
                raise UploadError(f"choose the {key.replace('_', ' ')} column")
            return val

        def names(key: str) -> tuple[str, ...]:
            val = payload.get(key) or []
            if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                raise UploadError(f"mapping.{key} must be a list of column names")
            if len(val) > MAX_COLUMNS:
                raise UploadError(f"mapping.{key} has too many columns")
            return tuple(val)

        time_system = name("time_system")
        if time_system not in TIME_SYSTEMS:
            raise UploadError(
                f"time system {time_system!r} is not supported: declare the time column as BJD_TDB. "
                "JD_UTC cannot be converted without the observatory's location."
            )
        return cls(
            time=name("time"),
            flux=name("flux"),
            err=name("err"),
            time_system=time_system,
            covariates=names("covariates"),
            ancillary_covariates=names("ancillary_covariates"),
            join_primary=name("join_primary", required=False),
            join_ancillary=name("join_ancillary", required=False),
        )


@dataclass(frozen=True)
class Normalized:
    """The table the transit fit will read, plus non-fatal notes."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    covariates: tuple[str, ...]
    warnings: tuple[str, ...] = field(default=())


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode(name: str, data: bytes) -> str:
    """The upload's text. Only UTF-8 (ASCII included) is accepted."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"{name}: file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if b"\x00" in data:
        raise UploadError(f"{name}: not a text table (binary content)")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadError(f"{name}: not UTF-8 text (byte {exc.start})") from None


def _content_lines(text: str) -> list[tuple[int, str]]:
    """Non-blank lines with their 1-based line numbers."""
    out = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.strip():
            out.append((lineno, raw))
    return out


def _split_header(lines: list[tuple[int, str]]) -> tuple[int, list[tuple[int, str]]]:
    """Index of the header line: the last comment line before the first data
    line when every line before it is a comment, else the first line."""
    first_data = next((i for i, (_, s) in enumerate(lines) if not s.lstrip().startswith("#")), None)
    if first_data is None:
        raise UploadError("no data rows")
    if first_data == 0:
        return 0, lines
    # Only comments precede the first non-comment line. That line is the header
    # unless it is all numbers, in which case the last comment line is.
    candidate = lines[first_data][1]
    if _looks_numeric_row(candidate):
        return first_data - 1, lines
    return first_data, lines


def _looks_numeric_row(line: str) -> bool:
    cells = [c for c in re.split(r"[,\t;| ]+", line.strip()) if c]
    return bool(cells) and all(_parse_float(c) is not None for c in cells)


def _detect_delimiter(header: str) -> str | None:
    """The header's delimiter, or None for whitespace-separated columns."""
    counts = {d: header.count(d) for d in _DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else None


def _split(line: str, delimiter: str | None) -> list[str]:
    if delimiter is None:
        return line.split()
    return [c.strip() for c in next(csv.reader(io.StringIO(line), delimiter=delimiter))]


def parse_table(name: str, data: bytes) -> Table:
    """Parse an uploaded table (CSV, TSV, semicolon, pipe or whitespace).

    Refuses a table with no header, blank or repeated column names, a row
    whose cell count differs from the header's, or too many rows or columns.
    """
    try:
        return _parse_table(name, data)
    except csv.Error as exc:  # e.g. a delimited field over the csv module's size limit
        raise UploadError(f"{name}: cannot read the delimited text ({exc})") from None


def _parse_table(name: str, data: bytes) -> Table:
    text = decode(name, data)
    lines = _content_lines(text)
    if not lines:
        raise UploadError(f"{name}: file is empty")
    try:
        hidx, lines = _split_header(lines)
    except UploadError as exc:
        raise UploadError(f"{name}: {exc}") from None
    header_line = lines[hidx][1].lstrip().lstrip("#").strip()
    delimiter = _detect_delimiter(header_line)
    columns = tuple(_split(header_line, delimiter))
    if len(columns) > MAX_COLUMNS:
        raise UploadError(f"{name}: more than {MAX_COLUMNS} columns")
    if any(not c for c in columns):
        raise UploadError(f"{name}: the header has a blank column name")
    dupes = sorted({c for c in columns if columns.count(c) > 1})
    if dupes:
        raise UploadError(f"{name}: repeated column name(s): {', '.join(dupes)}")
    if all(_parse_float(c) is not None for c in columns):
        raise UploadError(f"{name}: the first line holds numbers, not column names; add a header row")

    rows = []
    for lineno, raw in lines[hidx + 1:]:
        if raw.lstrip().startswith("#"):
            continue
        cells = tuple(_split(raw, delimiter))
        if len(cells) != len(columns):
            raise UploadError(
                f"{name}: line {lineno} has {len(cells)} cells but the header has {len(columns)}"
            )
        rows.append(cells)
        if len(rows) > MAX_ROWS:
            raise UploadError(f"{name}: more than {MAX_ROWS} rows")
        if len(rows) * len(columns) > MAX_CELLS:
            raise UploadError(f"{name}: more than {MAX_CELLS} cells; drop unused columns")
    if not rows:
        raise UploadError(f"{name}: no data rows")
    return Table(name=name, columns=columns, rows=tuple(rows))


def describe(table: Table, preview_rows: int = 5) -> dict:
    """What the mapping form shows: the columns, a few rows, and which columns
    hold numbers in every row (candidates for time, flux and error)."""
    numeric = [
        col for i, col in enumerate(table.columns)
        if all(_parse_float(r[i]) is not None for r in table.rows)
    ]
    return {
        "name": table.name,
        "columns": list(table.columns),
        "numeric_columns": numeric,
        "n_rows": len(table.rows),
        "preview": [list(r) for r in table.rows[:preview_rows]],
    }


# --------------------------------------------------------------------------
# Mapping and join
# --------------------------------------------------------------------------


def _parse_float(cell: str) -> float | None:
    """A finite float, or None for anything else (text, nan, inf)."""
    try:
        val = float(cell)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _is_missing(cell: str) -> bool:
    return cell.strip().casefold() in _MISSING_CELLS


def _column(table: Table, col: str, role: str) -> int:
    try:
        return table.columns.index(col)
    except ValueError:
        raise UploadError(f"{table.name}: no column {col!r} for {role}") from None


def _required_floats(table: Table, col: str, role: str) -> list[float]:
    idx = _column(table, col, role)
    out = []
    for n, row in enumerate(table.rows, start=1):
        val = _parse_float(row[idx])
        if val is None:
            raise UploadError(
                f"{table.name}: {role} column {col!r} row {n} is {row[idx]!r}, not a finite number"
            )
        out.append(val)
    return out


def _covariate_values(table: Table, col: str) -> list[float | None]:
    idx = _column(table, col, "a covariate")
    out: list[float | None] = []
    for n, row in enumerate(table.rows, start=1):
        cell = row[idx]
        if _is_missing(cell):
            out.append(None)
            continue
        val = _parse_float(cell)
        if val is None:
            raise UploadError(f"{table.name}: covariate {col!r} row {n} is {cell!r}, not a number")
        out.append(val)
    return out


def _join_index(primary: Table, ancillary: Table, mapping: Mapping) -> list[int]:
    """For each primary row, the ancillary row with the same key value.

    Keys match by exact numeric value (so ``2460000.5`` and ``2460000.50``
    agree) or, for a non-numeric key, by exact text. Every row of both files
    must match exactly one row of the other.
    """
    if not mapping.join_primary or not mapping.join_ancillary:
        raise UploadError("choose the column to join on in each file")

    def keys(table: Table, col: str) -> list[float | str]:
        idx = _column(table, col, "the join key")
        cells = [r[idx].strip() for r in table.rows]
        floats = [_parse_float(c) for c in cells]
        return floats if all(v is not None for v in floats) else cells

    pkeys = keys(primary, mapping.join_primary)
    akeys = keys(ancillary, mapping.join_ancillary)
    if type(pkeys[0]) is not type(akeys[0]):
        raise UploadError(
            f"join keys {mapping.join_primary!r} and {mapping.join_ancillary!r} are not comparable: "
            "one is numeric in every row and the other is not"
        )
    for table, col, ks in ((primary, mapping.join_primary, pkeys), (ancillary, mapping.join_ancillary, akeys)):
        seen: set = set()
        dupes = []
        for k in ks:
            if k in seen:
                dupes.append(k)
            seen.add(k)
        if dupes:
            raise UploadError(
                f"{table.name}: join key {col!r} repeats {len(dupes)} value(s), e.g. {dupes[0]!r}; "
                "each row must have a unique key"
            )
    where = {k: i for i, k in enumerate(akeys)}
    unmatched_primary = [k for k in pkeys if k not in where]
    unmatched_ancillary = len(set(akeys) - set(pkeys))
    if unmatched_primary or unmatched_ancillary:
        example = f", e.g. {unmatched_primary[0]!r}" if unmatched_primary else ""
        raise UploadError(
            f"the files do not join exactly: {len(unmatched_primary)} row(s) of {primary.name} "
            f"and {unmatched_ancillary} row(s) of {ancillary.name} have no match{example}"
        )
    return [where[k] for k in pkeys]


def _check_roles(primary: Table, mapping: Mapping, ancillary: Table | None) -> None:
    roles = (mapping.time, mapping.flux, mapping.err)
    if len(set(roles)) != 3:
        raise UploadError("time, flux and error must be three different columns")
    for col in mapping.covariates:
        if col in roles:
            raise UploadError(f"column {col!r} is already used as time, flux or error")
    if ancillary is None and (mapping.ancillary_covariates or mapping.join_ancillary):
        raise UploadError("ancillary covariates were chosen but no second file was uploaded")
    if ancillary is not None and not mapping.ancillary_covariates:
        raise UploadError("a second file was uploaded but none of its columns was chosen as a covariate")
    if mapping.join_ancillary and mapping.join_ancillary in mapping.ancillary_covariates:
        raise UploadError(f"join key {mapping.join_ancillary!r} cannot also be a covariate")
    names = list(mapping.covariates) + list(mapping.ancillary_covariates)
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise UploadError(f"covariate name(s) chosen twice or in both files: {', '.join(dupes)}")
    clash = sorted(set(names) & set(RESERVED_COLUMNS))
    if clash:
        raise UploadError(
            f"covariate name(s) {', '.join(clash)} would collide with the transit fit's own columns; "
            "rename the column in the file"
        )


def _fmt(val: float) -> str:
    """Shortest text that round-trips the float exactly."""
    return repr(val)


def normalize(primary: Table, mapping: Mapping, ancillary: Table | None = None) -> Normalized:
    """The transit-fit table built from *primary* (and *ancillary*) by *mapping*."""
    if mapping.time_system not in TIME_SYSTEMS:
        raise UploadError(f"time system {mapping.time_system!r} is not supported")
    _check_roles(primary, mapping, ancillary)

    times = _required_floats(primary, mapping.time, "time")
    low = min(times)
    if low < _MIN_FULL_JD:
        raise UploadError(
            f"time column {mapping.time!r} starts at {low}, which is not a full BJD_TDB "
            "(an offset time such as BTJD or BJD - 2457000?). Upload full BJD_TDB values."
        )
    flux = _required_floats(primary, mapping.flux, "flux")
    err = _required_floats(primary, mapping.err, "error")
    bad_err = next((n for n, e in enumerate(err, start=1) if e <= 0), None)
    if bad_err is not None:
        raise UploadError(f"error column {mapping.err!r} row {bad_err} is not positive")

    covs: list[tuple[str, list[float | None]]] = [
        (col, _covariate_values(primary, col)) for col in mapping.covariates
    ]
    if ancillary is not None:
        order = _join_index(primary, ancillary, mapping)
        for col in mapping.ancillary_covariates:
            values = _covariate_values(ancillary, col)
            covs.append((col, [values[i] for i in order]))

    warnings = []
    for col, values in covs:
        missing = sum(v is None for v in values)
        if missing == len(values):
            raise UploadError(
                f"covariate {col!r} has no numeric value in any row; timer would drop every row. "
                "Leave it out of the mapping."
            )
        if missing:
            warnings.append(f"covariate {col!r} is missing in {missing} row(s); timer drops those rows")

    columns = (*RESERVED_COLUMNS, *(c for c, _ in covs))
    rows = tuple(
        (_fmt(times[i]), _fmt(flux[i]), _fmt(err[i]),
         *("nan" if v[i] is None else _fmt(v[i]) for _, v in covs))
        for i in range(len(times))
    )
    return Normalized(columns=columns, rows=rows, covariates=tuple(c for c, _ in covs),
                      warnings=tuple(warnings))


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


def external_root() -> Path:
    """``<prose output>/external``: every uploaded product lives below it."""
    from muscat_db import photometry

    return photometry.output_base() / EXTERNAL_INSTRUMENT


def _night_start_jd(date: str) -> float:
    day = datetime.datetime.strptime(date, "%y%m%d").replace(tzinfo=datetime.timezone.utc)
    return day.timestamp() / 86400.0 + _UNIX_EPOCH_JD


def _check_night(date: str, times: tuple[str, ...]) -> None:
    first = min(float(t) for t in times)
    offset = first - _night_start_jd(date)
    if abs(offset) > _MAX_NIGHT_OFFSET_DAYS:
        raise UploadError(
            f"the first timestamp ({first:.5f}) is {offset:+.2f} days from night {date}; "
            "check the night and that the time column is BJD_TDB"
        )


def find_checksum(sha256: str) -> Path | None:
    """The product directory already holding a primary file with *sha256*."""
    root = external_root()
    if not root.is_dir():
        return None
    for path in root.glob(f"*/_runs/*/{RUN_PREFIX}*/{product_record.RECORD_NAME}"):
        record = product_record.read_record(path.parent)
        if record and record_primary_checksum(record) == sha256:
            return path.parent
    return None


def record_primary_checksum(record: dict) -> str | None:
    for entry in record.get("inputs") or []:
        if isinstance(entry, dict) and entry.get("kind") == "upload":
            for f in entry.get("files") or []:
                if isinstance(f, dict) and f.get("role") == "primary":
                    return str(f.get("sha256") or "") or None
    return None


def _clean_facility(facility: Any) -> str:
    if not isinstance(facility, str) or not facility.strip():
        raise UploadError("name the facility (telescope or survey) the light curve came from")
    text = " ".join(facility.split())
    if len(text) > MAX_FACILITY_LEN or not text.isprintable():
        raise UploadError(f"facility must be printable text of at most {MAX_FACILITY_LEN} characters")
    return text


def _file_name(name: Any) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    return base[:255] or "upload"


def validate_component_bytes(name: str, label: str) -> None:
    """Refuse names that cannot be stored on a filesystem with NAME_MAX=255."""
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        raise UploadError(f"{label} must be valid UTF-8 text") from None
    if size > MAX_COMPONENT_BYTES:
        raise UploadError(f"{label} must fit in {MAX_COMPONENT_BYTES} UTF-8 bytes")


def lightcurve_name(target: str, band: str, date: str, run_id: str) -> str:
    """The normalized CSV's name: the photometry convention the transit fit
    parses (``<target>_<inst>_<band>_<date>...``)."""
    return f"{jobs.target_dir_name(target)}_{EXTERNAL_INSTRUMENT}_{band}_{date}_{run_id}.csv"


def _write_csv(path: Path, norm: Normalized) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(norm.columns)
        writer.writerows(norm.rows)
    os.replace(tmp, path)


def save_upload(
    *,
    target: str,
    date: str,
    band: str,
    facility: str,
    mapping: Mapping,
    primary_name: str,
    primary_data: bytes,
    ancillary_name: str | None = None,
    ancillary_data: bytes | None = None,
    author: str | None = None,
    can_see: Callable[[str, str, str], bool] | None = None,
) -> dict:
    """Validate, normalize and store one upload as an external product.

    Returns ``{"inst", "date", "target", "run_id", "file", "warnings"}``.
    Raises :class:`UploadError` for anything refused; nothing is written then.

    *can_see* takes ``(date, target, run_id)`` of an existing upload and says
    whether the submitter may see it; the duplicate refusal names the earlier
    upload only when they may. Without it, every upload is named.
    """
    from muscat_db import photometry

    if not isinstance(date, str) or not _DATE_RE.match(date) or not photometry.valid_date(date):
        raise UploadError("night must be a 6-digit yymmdd date")
    try:
        _night_start_jd(date)  # valid_date checks the shape only, not 261399
    except ValueError:
        raise UploadError(f"night {date} is not a calendar date (yymmdd)") from None
    try:
        target_dir = jobs.target_dir_name(target)
    except ValueError:
        raise UploadError("target is required and must be a plain name") from None
    if len(target_dir) > MAX_TARGET_LEN or not target_dir.isprintable():
        raise UploadError(
            f"target must be printable text of at most {MAX_TARGET_LEN} characters"
        )
    if band not in BANDS:
        raise UploadError(f"band must be one of: {', '.join(BANDS)}")
    facility = _clean_facility(facility)
    primary_name = _file_name(primary_name)

    primary = parse_table(primary_name, primary_data)
    ancillary = None
    if ancillary_data is not None:
        ancillary_name = _file_name(ancillary_name)
        ancillary = parse_table(ancillary_name, ancillary_data)
    norm = normalize(primary, mapping, ancillary)
    _check_night(date, tuple(r[0] for r in norm.rows))

    primary_sha = checksum(primary_data)
    existing = find_checksum(primary_sha)
    if existing is not None:
        where = (existing.parent.parent.parent.name, existing.parent.name, existing.name)
        if can_see is None or can_see(*where):
            raise UploadError(f"{primary_name} was already uploaded (same checksum) as {'/'.join(where)}")
        raise UploadError(f"{primary_name} was already uploaded (same checksum)")

    run_id = f"{RUN_PREFIX}{primary_sha[:_CHECKSUM_PREFIX_LEN]}"
    rdir = photometry.run_output_dir(EXTERNAL_INSTRUMENT, date, target_dir, run_id)

    files = [{"role": "primary", "name": primary_name, "sha256": primary_sha, "bytes": len(primary_data)}]
    if ancillary is not None and ancillary_data is not None:
        files.append({"role": "ancillary", "name": ancillary.name,
                      "sha256": checksum(ancillary_data), "bytes": len(ancillary_data)})
    record = product_record.build_record(
        "photometry",
        author=author,
        inputs=[{"kind": "upload", "facility": facility, "target": target_dir, "files": files}],
        settings={
            "source": "upload",
            "facility": facility,
            "band": band,
            "mapping": asdict(mapping),
            "covariates": list(norm.covariates),
            "n_rows": len(norm.rows),
            "parser_version": PARSER_VERSION,
        },
        tool_versions={"muscat_db": __version__},
    )

    csv_name = lightcurve_name(target_dir, band, date, run_id)
    # Atomic writing adds .tmp: validate that longer component before mkdir.
    validate_component_bytes(csv_name + ".tmp", "generated CSV filename")
    try:
        rdir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UploadError(f"could not create the upload directory ({exc.strerror or exc})") from None
    try:
        # Atomic claim: a concurrent save of the same file (a double submit)
        # loses here rather than both passing the checksum scan above.
        rdir.mkdir()
    except FileExistsError:
        raise UploadError(f"an upload already occupies {run_id} for {target_dir} on {date}") from None
    except OSError as exc:
        raise UploadError(f"could not create the upload directory ({exc.strerror or exc})") from None
    try:
        _write_csv(rdir / csv_name, norm)
        if not product_record.write_record(rdir, record):
            raise UploadError("could not write the product record; nothing was saved")
    except BaseException:
        shutil.rmtree(rdir, ignore_errors=True)
        raise
    return {
        "inst": EXTERNAL_INSTRUMENT,
        "date": date,
        "target": target_dir,
        "run_id": run_id,
        "file": csv_name,
        "warnings": list(norm.warnings),
    }


def upload_covariates(csv_path: Path) -> tuple[str, ...]:
    """The covariates an uploaded light curve's record names, else ``()``.

    The transit fit keeps only a fixed allow-list of prose's columns
    (transit_fit._TIMER_COVARIATE_COLUMNS); an upload's covariates have the
    uploader's names and were chosen deliberately, so they pass through.
    """
    record = product_record.read_record(Path(csv_path).parent)
    if not record or record.get("level") != "photometry":
        return ()
    settings = record.get("settings") or {}
    if settings.get("source") != "upload":
        return ()
    covs = settings.get("covariates") or []
    return tuple(c for c in covs if isinstance(c, str) and c not in RESERVED_COLUMNS)
