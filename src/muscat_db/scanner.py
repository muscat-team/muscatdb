from __future__ import annotations

import csv
import logging
import os
import pathlib
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta

from muscat_db import scan_failures
from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE, InstrumentConfig

logger = logging.getLogger(__name__)


def _csv_has_valid_data(csv_path: str) -> bool:
    """False for a CSV an interrupted scan could plausibly have produced.

    A killed/crashed scan_date() can leave a CSV with a header but zero rows
    (killed before any file finished processing), or with rows whose OBJECT
    is blank (_read_fits_header_raw's corrupt/truncated-header fallback
    returns blank values for every requested key) -- so a data row that
    exists is not itself proof the file behind it was read successfully.
    """
    try:
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return False
    if not rows:
        return False
    return all((row.get("OBJECT") or "").strip() for row in rows)


def _obsdate_dir_is_complete(obslog_dir: str, d: str) -> bool:
    """True only if every CSV already written for this date looks complete.

    A CCD with zero FITS files that night legitimately has no CSV at all (see
    scan_date's per-CCD write loop), so requiring every *possible* CCD's CSV
    to exist would misflag genuinely-done dates. Requiring at least one CSV,
    and every CSV present to pass _csv_has_valid_data, catches an interrupted
    scan without that false positive.
    """
    date_dir = f"{obslog_dir}/{d}"
    try:
        csv_names = [f for f in os.listdir(date_dir) if f.endswith(".csv")]
    except (PermissionError, OSError):
        return False
    if not csv_names:
        return False
    return all(_csv_has_valid_data(f"{date_dir}/{f}") for f in csv_names)


def _is_obsdate_dir(name: str) -> bool:
    """True only for a canonical YYMMDD directory name."""
    if len(name) != 6 or not name.isdigit():
        return False
    try:
        datetime.strptime(name, "%y%m%d")
    except ValueError:
        return False
    return True

# FITS header blocks are 2880 bytes; almost all real headers fit in <=8 blocks.
_FITS_HEADER_MAX_BYTES = 2880 * 16

# A single-CCD instrument (sinistro, sbig, qhy600) has no sibling CCD to prove
# a date directory is genuinely readable, so an existing CSV's own mtime is
# used as "the last time this CCD/date was confirmed non-empty" (see
# _maybe_remove_stale_single_ccd_csv). 72h is evidence-based, not borrowed by
# analogy from MUSCAT_PHOT_FINALIZE_GRACE_S: the longest real per-night
# archive-delivery span observed on production data for these three
# instruments (checked directly against /data, excluding a handful of
# multi-month gaps traced to later bulk BANZAI re-reduction touching
# already-delivered nights, which is a different mechanism and never makes
# scan_date see zero matches) is ~62h -- itself one of #81's own
# still-unresolved misfile dates (sinistro 260730). 72h keeps ~10h of
# headroom over that worst observed case; see #115.
_DEFAULT_STALE_CSV_GRACE_S = 72 * 60 * 60


def _normalize_numeric(val: str) -> str:
    """Round-trip a numeric FITS value to match astropy formatting.

    FITS cards without a decimal point or exponent are typed as integers by
    astropy; preserve that distinction so downstream comparisons (e.g.
    ``read_mode == "1"``) keep working.
    """
    try:
        f = float(val)
    except ValueError:
        return val
    if "." not in val and "e" not in val and "E" not in val:
        return str(int(f))
    return str(f)


def _parse_fits_cards(text: str, wanted: set[str]) -> tuple[dict[str, str], bool]:
    """Parse 80-char FITS header cards. Returns (values, end_found)."""
    result: dict[str, str] = {}
    end_found = False
    for i in range(0, len(text) - 79, 80):
        card = text[i:i + 80]
        key = card[:8].strip()
        if key == "END":
            end_found = True
            break
        if key not in wanted or card[8:10] != "= ":
            continue
        val_part = card[10:]
        if val_part.lstrip().startswith("'"):
            stripped = val_part.lstrip()
            end_quote = stripped.find("'", 1)
            val = stripped[1:end_quote] if end_quote > 0 else stripped[1:]
            result[key] = val.strip()
        else:
            slash = val_part.find("/")
            val = (val_part[:slash] if slash >= 0 else val_part).strip()
            result[key] = _normalize_numeric(val)
    return result, end_found


def _read_fits_header_raw(filepath: str, keys: list[str]) -> dict[str, str] | None:
    """Fast path: read FITS primary-HDU header cards directly from disk.

    Returns ``None`` to signal that astropy should be tried (well-formed primary
    HDU with no requested keys — typical of MEF files). Returns an empty-values
    dict for corrupt files (no ``END`` card within the first ~46 KB) so the
    caller skips the file instead of feeding it to astropy, which can hang on
    pathological headers.
    """
    try:
        with open(filepath, "rb") as f:
            data = f.read(_FITS_HEADER_MAX_BYTES)
    except OSError:
        return None
    if len(data) < 80 or not data.startswith(b"SIMPLE  ="):
        return None
    text = data.decode("ascii", errors="replace")
    values, end_found = _parse_fits_cards(text, set(keys))
    if values:
        # Got at least one requested key — use whatever we parsed.
        return {k: values.get(k, "") for k in keys}
    if end_found:
        # Well-formed primary HDU with none of our keys → MEF, try astropy.
        return None
    # No END card and no values → corrupt/truncated header. Return empties so
    # the caller skips it; astropy is liable to hang on these.
    return {k: "" for k in keys}


def _read_fits_header_astropy(filepath: str, keys: list[str]) -> dict[str, str]:
    """Fallback: full astropy parse, including MEF extension scan."""
    from astropy.io import fits  # imported lazily so workers don't pay for it
    result: dict[str, str] = {k: "" for k in keys}
    try:
        with fits.open(filepath, memmap=False) as hdul:
            for hdu in hdul:
                header = hdu.header
                for key in keys:
                    if not result[key]:
                        try:
                            val = header[key]
                            if val is not None and str(val).strip():
                                result[key] = str(val).strip()
                        except (KeyError, ValueError):
                            pass
                if all(result.values()):
                    break
    except Exception:
        logger.debug("astropy fallback failed reading FITS header %s", filepath, exc_info=True)
    return result


def _read_fits_header_keys(filepath: str, keys: list[str]) -> dict[str, str]:
    raw = _read_fits_header_raw(filepath, keys)
    if raw is not None:
        return raw
    # Only MEF-like files (well-formed primary HDU, no requested keys) reach
    # this path; corrupt files are skipped by the raw parser above.
    return _read_fits_header_astropy(filepath, keys)


def _process_single_file(filepath: str, inst: InstrumentConfig) -> dict[str, str] | None:
    fname = os.path.basename(filepath).removesuffix(".fits")
    kv = _read_fits_header_keys(filepath, inst.keys)
    mjd_key = "MJD-OBS" if inst.use_alt_ut_key else "MJD-STRT"
    ut_key = "UTSTART" if inst.use_alt_ut_key else "EXP-STRT"
    try:
        mjd = float(kv.get(mjd_key, "0"))
    except ValueError:
        mjd = 0.0
    jd = mjd - 49999.5
    ut_raw = kv.get(ut_key, "")
    ut_parts = ut_raw.split(":")
    ut = f"{ut_parts[0]}:{ut_parts[1]}:{int(float(ut_parts[2])):02d}" if len(ut_parts) >= 3 else ut_raw
    read_mode = kv.get("SPDTAB" if not inst.use_alt_ut_key else "CONFMODE", "")
    read_mode = "high" if read_mode == "1" else ("low" if read_mode == "0" else read_mode)
    airmass_key = inst.airmass_key
    try:
        focus_val = float(kv.get("FOC-VAL" if not inst.use_alt_ut_key else "FOCPOSN", "0"))
    except ValueError:
        focus_val = 0.0
    row = {
        "FRAME": fname,
        "OBJECT": kv.get("OBJECT", ""),
        "JD-STRT": f"{jd:.6f}",
        "UT-STRT": ut,
        "EXPTIME (s)": kv.get("EXPTIME", ""),
        "READ_MODE": read_mode,
        "FILTER": kv.get("FILTER", ""),
        "RA": kv.get("RA", ""),
        "DEC": kv.get("DEC", ""),
        airmass_key: kv.get(airmass_key, ""),
        inst.focus_label: f"{focus_val:.3f}" if focus_val else kv.get("FOC-VAL" if not inst.use_alt_ut_key else "FOCPOSN", ""),
    }
    if inst.has_pa:
        row["PA (deg)"] = kv.get("INST-PA", "")
    if "PROPID" in inst.keys:
        row["PROPID"] = kv.get("PROPID", "")
    return row


def _date_dir(
    inst: InstrumentConfig,
    obsdate: str,
    data_root: str | os.PathLike[str] | None = None,
) -> str:
    if data_root is None:
        instrument_dir = pathlib.Path(inst.data_dir)
    else:
        instrument_dir = pathlib.Path(data_root).expanduser() / inst.data_subdir
    return str(instrument_dir / obsdate)


def _find_fits_files(
    inst: InstrumentConfig,
    obsdate: str,
    ccd: int,
    data_root: str | os.PathLike[str] | None = None,
) -> list[str]:
    datadir = _date_dir(inst, obsdate, data_root)
    if not os.path.isdir(datadir):
        return []
    if inst.ep_names:
        eps = inst.ep_names[ccd]
        eps = (eps,) if isinstance(eps, str) else eps
        try:
            matches = sorted(
                {p for ep in eps for p in pathlib.Path(datadir).glob(f"{inst.prefix}{ep}*e91.fits")}
            )
        except (PermissionError, OSError):
            return []
    else:
        pattern = f"{inst.prefix}{ccd}*.fits"
        try:
            matches = sorted(pathlib.Path(datadir).glob(pattern))
        except (PermissionError, OSError):
            return []
    return [str(p) for p in matches]


_EPOCH_RE = re.compile(r"(ep\d+)-")


def _unscanned_frames(inst: InstrumentConfig, datadir: str) -> tuple[Counter, int]:
    """Frames in *datadir* that the per-CCD globs in :func:`_find_fits_files` skip.

    Returns ``(unknown_epochs, fz_only)``: a count of unpacked e91 frames per
    epoch token that no CCD lists, and the number of ``*e91.fits.fz`` frames
    that have no unpacked ``.fits`` beside them. Only names starting with the
    instrument's prefix count, so another telescope's frames filed in the same
    directory are not reported.
    """
    known = {
        ep for entry in inst.ep_names for ep in ((entry,) if isinstance(entry, str) else entry)
    }
    names = os.listdir(datadir)
    present = set(names)
    unknown: Counter = Counter()
    fz_only = 0
    for name in names:
        if not name.startswith(inst.prefix):
            continue
        if name.endswith("e91.fits.fz"):
            if name[:-3] not in present:
                fz_only += 1
        elif name.endswith("e91.fits") and known != {""}:
            match = _EPOCH_RE.match(name[len(inst.prefix):])
            if match and match.group(1) not in known:
                unknown[match.group(1)] += 1
    return unknown, fz_only


def _warn_unscanned_frames(
    inst_name: str,
    obsdate: str,
    data_root: str | os.PathLike[str] | None,
) -> None:
    """Say so when frames in the date directory will not be scanned (#197, #198).

    Both cases used to be silent: a rescan simply never saw the frames, so a
    forced one could replace a correct CSV with fewer rows or none.
    """
    inst = INSTRUMENTS[inst_name]
    if not inst.ep_names:
        return
    datadir = _date_dir(inst, obsdate, data_root)
    try:
        unknown, fz_only = _unscanned_frames(inst, datadir)
    except OSError as exc:
        logger.debug("cannot list %s for unscanned frames: %s", datadir, exc)
        return
    if unknown:
        detail = ", ".join(f"{ep}: {n}" for ep, n in sorted(unknown.items()))
        print(
            f"[warn] {inst_name} {obsdate}: {sum(unknown.values())} frame(s) with an "
            f"unrecognised epoch name were not scanned ({detail}); add the name to "
            f"instruments.py or every rescan will keep missing them"
        )
    if fz_only:
        print(
            f"[warn] {inst_name} {obsdate}: {fz_only} .fits.fz frame(s) have no unpacked "
            f".fits and were not scanned; run funpack on them (scanning and photometry "
            f"both need the .fits)"
        )


def _stale_csv_grace_seconds() -> float:
    return float(os.environ.get("MUSCAT_SCAN_STALE_CSV_GRACE_S", _DEFAULT_STALE_CSV_GRACE_S))


def _maybe_remove_stale_single_ccd_csv(inst_name: str, obsdate: str) -> None:
    """Remove a single-CCD instrument's obslog CSV once it is confirmed stale.

    Only called when this scan found zero matches for a single-CCD instrument
    (inst.nccd == 1) -- multi-CCD instruments are already covered by the
    sibling-CCD proof in scan_date's main loop.
    """
    csv_path = f"{OBSLOG_BASE}/{inst_name}/{obsdate}/obslog-{inst_name}-{obsdate}-ccd0.csv"
    if not os.path.isfile(csv_path):
        return
    try:
        age_s = time.time() - os.path.getmtime(csv_path)
    except OSError as e:
        print(f"[warn] cannot stat {csv_path}: {e}")
        return
    grace_s = _stale_csv_grace_seconds()
    if age_s < grace_s:
        return
    try:
        os.remove(csv_path)
    except OSError as e:
        print(f"[warn] cannot remove stale {csv_path}: {e}")
        return
    print(
        f"[info] removed stale obslog CSV for {inst_name} {obsdate}: "
        f"no matches for {age_s / 3600:.1f}h, past the {grace_s / 3600:.1f}h grace window"
    )


def scan_date(
    inst_name: str,
    obsdate: str,
    max_workers: int | None = None,
    progress=None,
    data_root: str | os.PathLike[str] | None = None,
) -> dict:
    """Scan all CCDs for a date.

    Returns {"total": int, "per_ccd": {ccd: count}} — falsy if no files found.

    Every outcome is reflected in the scan-failure ledger (issue #196): an
    exception, or a CCD whose CSV could not be written, records the date; a
    scan that writes every CSV clears it. Done here rather than in each caller
    so the cron, CLI, LCO monitor, web and PROPID-backfill paths all report
    alike. A zero-file result leaves an existing entry alone -- no files is
    not proof the earlier failure was resolved (the directory may be gone).
    """
    try:
        result = _scan_date(inst_name, obsdate, max_workers, progress, data_root)
    except Exception as exc:
        scan_failures.record(OBSLOG_BASE, inst_name, obsdate, f"{type(exc).__name__}: {exc}")
        raise
    if result.get("write_errors"):
        scan_failures.record(
            OBSLOG_BASE, inst_name, obsdate, "; ".join(result["write_errors"]),
        )
        if "total" not in result:
            return {}  # nothing was written; callers read truthy as "had data"
    elif result:
        scan_failures.clear(OBSLOG_BASE, inst_name, obsdate)
    return result


def _scan_date(
    inst_name: str,
    obsdate: str,
    max_workers: int | None,
    progress,
    data_root: str | os.PathLike[str] | None,
) -> dict:
    inst = INSTRUMENTS[inst_name]

    file_ccd_pairs: list[tuple[str, int]] = []
    for ccd in range(inst.nccd):
        for fp in _find_fits_files(inst, obsdate, ccd, data_root=data_root):
            file_ccd_pairs.append((fp, ccd))

    _warn_unscanned_frames(inst_name, obsdate, data_root)

    if not file_ccd_pairs:
        # Returned falsy either way, even when a stale CSV is removed below:
        # scan_date_for_all_inst/scan_yesterday treat any truthy result as
        # "this instrument had real data," and a removal here is cleanup, not
        # data -- see _maybe_remove_stale_single_ccd_csv and #115.
        #
        # Gated to data_root is None (the canonical MUSCAT_DATA_DIR scan path):
        # the removal always targets the one canonical OBSLOG_BASE CSV, but the
        # zero-match proof above is scoped to whichever data_root this call
        # searched. lco_monitor.py/lco.py's archive-download scans pass
        # data_root=lco.download_root(), which resolves to MUSCAT_LCO_DIR when
        # configured -- a directory .env.example documents as legitimately
        # different from MUSCAT_DATA_DIR. Removing the canonical CSV on a
        # zero-match result from that *other* tree would delete a CSV that
        # still correctly describes real files under MUSCAT_DATA_DIR.
        if inst.nccd == 1 and data_root is None:
            _maybe_remove_stale_single_ccd_csv(inst_name, obsdate)
        return {}

    # Created only once there is something to write. scan_missing_dates()
    # treats this directory's existence as "already scanned" regardless of
    # whether it holds a CSV, so creating it unconditionally (the previous
    # behaviour) permanently hid any date whose archive delivery lagged past
    # the scan attempt: the marker directory outlives the empty result, and
    # nothing ever retries a date that already "exists".
    logdir = f"{OBSLOG_BASE}/{inst_name}/{obsdate}"
    try:
        os.makedirs(logdir, exist_ok=True)
    except (PermissionError, OSError) as e:
        print(f"[warn] cannot create {logdir}: {e}")
        return {"write_errors": [f"cannot create {logdir}: {e}"]}

    total = len(file_ccd_pairs)
    max_workers = max_workers or (os.cpu_count() or 4)
    rows_by_ccd: dict[int, list[dict[str, str]]] = {}

    task_id = None
    ccd_label = f"CCD0-{inst.nccd - 1}"
    if progress is not None:
        task_id = progress.add_task(
            f"[cyan]{inst_name} {obsdate} {ccd_label}[/]", total=total, filename=""
        )

    # CPU-bound header parsing dominates per-file cost, so processes scale where
    # threads can't (the GIL serialises the parse loop). Chunked map keeps
    # dispatch overhead low.
    paths = [fp for fp, _ in file_ccd_pairs]
    ccds  = [ccd for _, ccd in file_ccd_pairs]
    chunksize = max(1, total // (max_workers * 4))
    if max_workers == 1:
        # Automatic LCO ingestion runs from a monitor thread. Forking a process
        # pool from a multithreaded web server is unsafe; the default monitor
        # therefore uses this deterministic serial path. Operators can opt into
        # a larger worker count when their process-start method is configured
        # appropriately.
        processed = map(_process_single_file, paths, [inst] * total)
        executor = None
    else:
        executor = ProcessPoolExecutor(max_workers=max_workers)
        processed = executor.map(_process_single_file, paths, [inst] * total, chunksize=chunksize)
    try:
        for fp, ccd, row in zip(paths, ccds, processed):
            if row:
                rows_by_ccd.setdefault(ccd, []).append(row)
            if progress is not None:
                progress.update(task_id, advance=1, filename=os.path.basename(fp))
    finally:
        if executor is not None:
            executor.shutdown()

    # A CCD whose CSV cannot be written leaves the date looking complete to
    # scan-missing (the sibling CCDs' CSVs are fine), so it is never retried
    # on its own. Collected and returned so scan_date can record it.
    write_errors: list[str] = []
    for ccd in sorted(rows_by_ccd):
        csv_path = f"{logdir}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
        fieldnames = inst.csv_header.split(",")
        try:
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in sorted(rows_by_ccd[ccd], key=lambda r: r["FRAME"]):
                    writer.writerow({k: row.get(k, "") for k in fieldnames})
        except (PermissionError, OSError) as e:
            print(f"[warn] cannot write {csv_path}: {e}")
            write_errors.append(f"cannot write ccd{ccd} CSV {csv_path}: {e}")

    # A CCD with no rows this scan is not itself ambiguous: every CCD checked
    # here shares this date's one data directory, and `total > 0` (we're past
    # the early return above) already proves that directory exists and is
    # listable, via whichever sibling CCD did find matches. So a CCD landing
    # outside rows_by_ccd genuinely has nothing on disk right now, not a read
    # glitch, and any CSV still sitting there is a stale leftover from before
    # its frames moved elsewhere (#81) — remove it so a rebuild doesn't
    # re-ingest the pre-move split. This can't fire for a single-CCD
    # instrument: there, zero matches for its one CCD means total == 0 and we
    # never reach this point, so a stale single-CCD CSV needs a rescan of
    # whichever date the query in #81 identifies, not this loop.
    removed_ccds: list[int] = []
    for ccd in range(inst.nccd):
        if ccd in rows_by_ccd:
            continue
        csv_path = f"{logdir}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
        if not os.path.isfile(csv_path):
            continue
        try:
            os.remove(csv_path)
        except OSError as e:
            print(f"[warn] cannot remove stale {csv_path}: {e}")
            continue
        removed_ccds.append(ccd)

    return {
        "total": total,
        "per_ccd": {ccd: len(rows) for ccd, rows in rows_by_ccd.items()},
        "removed_ccds": removed_ccds,
        "write_errors": write_errors,
    }


def missing_dates(inst_name: str, year_prefix: str, force: bool = False) -> list[str]:
    """Raw date directories of *inst_name* that ``scan_missing_dates`` would scan.

    Same selection rules as there (see its docstring); split out so the
    periodic sweep can vet each date before rescanning it.
    """
    prefix = "" if year_prefix.lower() == "all" else year_prefix
    inst = INSTRUMENTS[inst_name]
    data_dir = inst.data_dir
    obslog_dir = f"{OBSLOG_BASE}/{inst_name}"
    existing = set()
    if not force and os.path.isdir(obslog_dir):
        try:
            entries = os.listdir(obslog_dir)
        except (PermissionError, OSError) as e:
            print(f"[warn] cannot list {obslog_dir}: {e}")
            entries = []
        for d in entries:
            if (
                os.path.isdir(f"{obslog_dir}/{d}") and d.startswith(prefix)
                and _obsdate_dir_is_complete(obslog_dir, d)
            ):
                existing.add(d)
    if not os.path.isdir(data_dir):
        return []
    try:
        data_entries = sorted(os.listdir(data_dir))
    except (PermissionError, OSError) as e:
        print(f"[warn] cannot list {data_dir}: {e}")
        return []
    return [
        d for d in data_entries
        if _is_obsdate_dir(d)
        and os.path.isdir(f"{data_dir}/{d}") and d.startswith(prefix) and d not in existing
    ]


def scan_missing_dates(
    inst_name: str,
    year_prefix: str,
    max_workers: int | None = None,
    progress=None,
    force: bool = False,
) -> list[str]:
    """Scan dates for an instrument.

    ``year_prefix`` filters date directories by leading characters (e.g. ``"25"``).
    Pass ``"all"`` (case-insensitive) to scan every date directory under the
    instrument's data dir.

    By default, only dates without a complete obslog CSV are scanned -- a
    marker directory with no CSV, an empty CSV, or a CSV holding a
    blank-OBJECT row (see _obsdate_dir_is_complete) counts as incomplete and
    is retried, since any of those can be left behind by a killed/crashed
    scan. With ``force=True``, every date with FITS data is rescanned,
    overwriting any existing CSVs — useful for fixing legacy malformed
    obslogs.
    """
    scanned: list[str] = []
    missing = missing_dates(inst_name, year_prefix, force=force)
    if not missing:
        return scanned
    prefix = "" if year_prefix.lower() == "all" else year_prefix
    task_id = None
    if progress is not None:
        label = "all" if prefix == "" else f"{prefix}xx"
        task_id = progress.add_task(
            f"[cyan]{inst_name} {label}[/]", total=len(missing), filename=""
        )
    for d in missing:
        try:
            scan_date(inst_name, d, max_workers=max_workers, progress=None)
            scanned.append(d)
        except (PermissionError, OSError) as e:
            print(f"[warn] skipping {inst_name} {d}: {e}")
        except Exception as e:
            print(f"[warn] {inst_name} {d} failed: {type(e).__name__}: {e}")
        if progress is not None:
            progress.update(task_id, advance=1, filename=d)
    return scanned


def scan_all_instruments(year_prefix: str, max_workers: int | None = None) -> dict[str, list[str]]:
    from rich.progress import (
        BarColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
    )
    result: dict[str, list[str]] = {}
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[bold]{task.fields[filename]}"),
        TimeRemainingColumn(),
    ) as progress:
        for name in INSTRUMENTS:
            dates = scan_missing_dates(name, year_prefix, max_workers=max_workers, progress=progress)
            if dates:
                result[name] = dates
    return result


def scan_date_for_all_inst(obsdate: str, max_workers: int | None = None) -> list[str]:
    from rich.progress import (
        BarColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
    )
    scanned: list[str] = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[bold]{task.fields[filename]}"),
        TimeRemainingColumn(),
    ) as progress:
        for name in INSTRUMENTS:
            try:
                result = scan_date(name, obsdate, max_workers=max_workers, progress=progress)
                if result:
                    scanned.append(name)
            except Exception:
                # WARNING, not DEBUG: nothing prints DEBUG, so this used to drop
                # an instrument's whole night without a trace (#196). scan_date
                # has already recorded it in the scan-failure ledger.
                logger.warning(
                    "scan_date failed for %s %s; recorded in %s for retry",
                    name, obsdate, scan_failures.ledger_path(OBSLOG_BASE), exc_info=True,
                )
    return scanned


def yesterday_obsdate() -> str:
    return (date.today() - timedelta(days=1)).strftime("%y%m%d")


def scan_yesterday(max_workers: int | None = None) -> list[str]:
    return scan_date_for_all_inst(yesterday_obsdate(), max_workers=max_workers)
