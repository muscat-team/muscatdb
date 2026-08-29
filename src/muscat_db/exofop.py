"""ExoFOP time-series cross-checking for the LCO archive page.

ExoFOP (https://exofop.ipac.caltech.edu) records ground-based ``time_series``
entries (typically TFOPWG transit follow-up reports) against a TESS/TOI target.
Each entry describes one reported observing run: telescope, camera, filter, the
UT date observed, the number of frames, and — uniquely useful here — the LCO
``tag``/request id (``tstag``) that produced it.

This module lets the LCO archive page answer "which reported ExoFOP time series
for this target do we already have in muscat-db, and which are missing?", so an
operator can fetch the ones we lack from the LCO archive.

The "already in the database" test is purely local (against the muscat-db frames
table) so the report makes no extra LCO archive API calls. The download pathway
reuses the existing request-id based archive search + background download job in
``lco.py`` (``tstag`` is an LCO observation request id).
"""

from __future__ import annotations

import datetime
import re
import threading

from muscat_db import catalog, lco

# How recently a fetched ExoFOP response is considered fresh enough to reuse
# (seconds). ExoFOP time series change slowly, so a short TTL lets an operator
# re-run a search in the same session without hammering the external service.
_EXOFOP_CACHE_TTL_S = 3600

_exofop_cache: dict = {}
_exofop_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# TOI number resolution
# --------------------------------------------------------------------------- #

_TOI_NAME_RE = re.compile(r"^TOI0*(\d+)(?:\.\d+)?$", re.IGNORECASE)
_PLAIN_TOI_RE = re.compile(r"^toi0*(\d+)$", re.IGNORECASE)


def _toi_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN -> None


def resolve_toi_number(target: str) -> str | None:
    """Return the TOI host number string (e.g. ``"6715"``) for *target*, or None.

    Accepts both explicit TOI names (``TOI-6715``, ``toi6715``) and any name that
    the local TOI/NExScI catalogs resolve to a known TOI target (e.g.
    ``TIC180695581`` which is TOI 6715). The returned value is the *host* TOI
    number without the candidate suffix, which is what ExoFOP's ``id=`` query
    expects (``https://exofop.ipac.caltech.edu/tess/target.php?id=TOI-6715``).
    """
    name = (target or "").strip()
    if not name:
        return None

    norm = catalog._normalize_target_name(name)
    # Direct TOI designation: "TOI-6715", "toi6715", "TOI01404", ...
    m = _TOI_NAME_RE.match(norm) or _PLAIN_TOI_RE.match(norm)
    if m and m.group(1):
        return str(int(m.group(1)))

    # A TIC id or a catalog alias: look the host up in the local TOI catalog.
    cat = _load_catalog()
    if cat is None:
        return None

    aliases = catalog._target_lookup_aliases(norm)
    if not aliases:
        return None

    toi_col = cat.get("toi") or []
    tic_col = cat.get("tic") or []
    name_col = cat.get("name") or []
    for i in range(len(toi_col)):
        toi = str(toi_col[i] or "").strip()
        if not toi:
            continue
        tic = str(tic_col[i] or "").strip()
        row_name = str(name_col[i] or "").strip()
        row_aliases = {catalog._normalize_target_name(row_name)} if row_name else set()
        if tic:
            row_aliases.add(catalog._normalize_target_name(f"TIC{re.sub(r'\\D', '', tic)}"))
        toi_num = _toi_float(toi)
        if toi_num is not None:
            row_aliases.add(f"TOI{int(toi_num)}")
            row_aliases.add(catalog._normalize_target_name(f"TOI-{toi}"))
        if not (aliases & row_aliases):
            continue
        if toi_num is not None:
            return str(int(toi_num))
    return None


def _load_catalog():
    """Return the column-oriented TOI catalog from catalog.py, or None."""
    try:
        return catalog._load_toi_catalog()["data"]
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# ExoFOP time_series fetch (with response-level caching)
# --------------------------------------------------------------------------- #

def _exofop_url(toi: str) -> str:
    return f"https://exofop.ipac.caltech.edu/tess/target.php?id=TOI-{toi}&json"


def fetch_time_series(toi: str, *, _now: float | None = None) -> list[dict]:
    """Return the ExoFOP ``time_series`` list for the TOI host number, cached."""
    toi = str(toi or "").strip()
    if not toi:
        return []
    cache_key = f"ts:{toi}"
    now = _now if _now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    with _exofop_cache_lock:
        hit = _exofop_cache.get(cache_key)
        if hit is not None and now - hit[0] < _EXOFOP_CACHE_TTL_S:
            return [dict(r) for r in hit[1]]

    try:
        resp = catalog._sync_get(
            _exofop_url(toi),
            headers={"User-Agent": "MuSCAT-db/0.1.0"},
            timeout=20,
        )
        data = resp.json()
    except Exception:
        return []

    entries = data.get("time_series") or []
    entries = [dict(e) for e in entries]
    with _exofop_cache_lock:
        _exofop_cache[cache_key] = (now, [dict(e) for e in entries])
    return entries


# --------------------------------------------------------------------------- #
# Derived observation fields (site / telescope / instrument / obsdate)
# --------------------------------------------------------------------------- #

# ExoFOP telescope-name tokens -> LCO site code. LCO's archive/DB are keyed by
# these three-letter site codes, so a report must be mapped onto them.
_SITE_TOKENS = {
    "SAAO": "cpt",
    "CTIO": "lsc",
    "OGG": "ogg",
    "HALEAKALA": "ogg",
    "COJ": "coj",
    "SIDING SPRING": "coj",
    "ELP": "elp",
    "MCDONALD": "elp",
    # Real ExoFOP tstel values abbreviate McDonald Observatory as "McD", not
    # the full "MCDONALD" (e.g. "LCO-McD-1m (1.0 m)", "LCO-McD (1.0 m)" --
    # confirmed against TOI-1807's reported LCO/Sinistro time-series entries).
    "MCD": "elp",
    "TFN": "tfn",
    "TEIDE": "tfn",
    "TLV": "tlv",
    "WISE": "tlv",
}

# Known 1 m / 0.4 m cameras in ExoFOP follow-up reports -> LCO-style INSTRUME.
_CAMERA_TO_INSTRUME = {
    "SINISTRO": "sinistro",
    "FA": "sinistro",
    "MUSCAT": "muscat",
    "QHY600": "qhy600",
    "SBIG": "sbig",
    "STL": "sbig",
}


def _site_from_tel(tstel: str) -> str:
    upper = (tstel or "").upper()
    for token, code in _SITE_TOKENS.items():
        if token in upper:
            return code
    # Fall back to an LCO-style site code embedded in the string (e.g. "1m0a").
    m = re.search(r"\b(cpt|lsc|ogg|coj|elp|tfn|tlv)\b", upper)
    return m.group(1) if m else ""


def _tel_class(tstel: str) -> str:
    # ExoFOP tstel strings mix a bare telescope-class token (e.g. "1m0") with a
    # decimal descriptive form (e.g. "(1.0 m)"), and either can appear alone
    # (compare the "LCO-SAAO (1 m)" vs "LCO-CTIO-1m0 (1.0 m)" test fixtures).
    # The decimal checks must run against the *unstripped* lowercased string:
    # matching them against a period-stripped token (as before) turns "0.4 m"
    # into dead code that can never match, since the literal "." it looks for
    # has already been removed.
    raw = (tstel or "").lower()
    token = raw.replace(".", "")
    if "0m4" in token or "0.4m" in raw or "0.4 m" in raw:
        return "0m4"
    if "1m0" in token or "1 m" in token or "1.0m" in raw or "1.0 m" in raw or ("1m" in token and "0m" not in token):
        return "1m0"
    if "2m0" in token or "2 m" in token or "2.0m" in raw or "2.0 m" in raw:
        return "2m0"
    return ""


def _instrument_from(entry: dict) -> str:
    """Infer the muscat-db instrument for an ExoFOP time-series entry.

    Reuses ``lco.infer_archive_instrument`` so the mapping is identical to the
    archive-download path (sinistro for 1 m, muscat3/muscat4 by 2 m site, sbig
    vs qhy600 for the 0.4 m generation).
    """
    tstel = str(entry.get("tstel") or "")
    site = _site_from_tel(tstel)
    tel = _tel_class(tstel)
    camera = str(entry.get("tscam") or "")
    instrume = ""
    for token, kind in _CAMERA_TO_INSTRUME.items():
        if token in camera.upper():
            instrume = kind
            break
    if not instrume:
        return ""
    try:
        return lco.infer_archive_instrument(
            {"SITEID": site, "TELID": tel, "INSTRUME": instrume}
        )
    except lco.LcoError:
        return ""


def _obsdate_window(tsdate: str) -> list[str]:
    """Candidate observing-night YYMMDD labels around an ExoFOP UT date.

    ``tsdate`` is the UT date of the run. The observing-night label stored in
    ``frames.obsdate`` can lag it by a day at sites whose night crosses local
    midnight, so return the three candidate labels around it and let the local
    matcher pick whichever matches by position.
    """
    try:
        d = datetime.date.fromisoformat(str(tsdate or "")[:10])
    except ValueError:
        return []
    return [(d + datetime.timedelta(days=delta)).strftime("%y%m%d") for delta in (-1, 0, 1)]


def _target_coords(target: str) -> tuple[float, float] | None:
    try:
        resolved = catalog._resolve_archive_coords(target)
    except Exception:
        return None
    if resolved is None:
        return None
    return float(resolved[0]), float(resolved[1])


# --------------------------------------------------------------------------- #
# Archive fallback search (when tstag doesn't resolve via request_id)
# --------------------------------------------------------------------------- #

def archive_fallback_search(
    target: str,
    tsdate: str,
    *,
    reduction_level: str = "91",
    user_name: str | None = None,
) -> list[dict]:
    """Search the LCO archive by target coordinates + date window.

    An ExoFOP time-series entry's ``tstag`` is documented as the LCO request
    id that produced it, and searching the archive by ``request_id=<tstag>``
    is the primary, precise download path. That assumption doesn't hold for
    at least some older TFOPWG reports though (observed: TOI-1807's 2020-era
    LCO/Sinistro rows all resolve to zero frames under ``request_id``, even
    though the same nights are still archived and findable by target+date --
    ``tstag`` there evidently predates the modern archive's request-id
    numbering). This is the same coordinate + date-window search the "Search
    LCO Archive" tab uses, so the one-click download still works once the
    precise path comes up empty. Returns ``[]`` (never raises) when the
    target's coordinates can't be resolved, ``tsdate`` can't be parsed, or
    the archive call itself fails, so callers can treat this purely as "try
    the fallback, then report not-found" without extra error handling.
    """
    coords = _target_coords(target)
    if coords is None:
        return []
    ra_deg, dec_deg = coords
    try:
        d = datetime.date.fromisoformat(str(tsdate or "")[:10])
    except ValueError:
        return []
    start = (d - datetime.timedelta(days=1)).isoformat()
    end = (d + datetime.timedelta(days=1)).isoformat()
    try:
        result = lco.archive_search_all(
            {
                "covers": f"POINT({ra_deg} {dec_deg})",
                "reduction_level": reduction_level,
                "start": start,
                "end": end,
                "limit": "1000",
            },
            user_name,
        )
    except lco.LcoError:
        return []
    return result.get("results") or []


# --------------------------------------------------------------------------- #
# Existence cross-check against muscat-db
# --------------------------------------------------------------------------- #

def check_time_series_exists(entry: dict, *, target: str = "") -> dict:
    """Return whether an ExoFOP time-series entry is already in muscat-db.

    The check is entirely local (no LCO call): infer the instrument and site,
    scan the frames table over a +/-1-day obsdate window for that instrument and
    site, and match the target's coordinates to the stored frame coordinates
    within the same 60-arcsec tolerance ``lco._annotate_lco_archive_results``
    uses. Returns the entry merged with ``exists`` / ``dataset_matched_object`` /
    ``dataset_local_frames``.
    """
    result = dict(entry)
    inst = _instrument_from(entry)
    site = _site_from_tel(str(entry.get("tstel") or ""))
    result["instrument"] = inst
    result["site"] = site

    if not inst or not site:
        result["exists"] = False
        result["exists_checked"] = False
        return result

    coords = _target_coords(target) if target else None
    if coords is None:
        result["exists"] = False
        result["exists_checked"] = False
        return result
    ra_deg, dec_deg = coords

    matching = lco.local_lco_dataset_match(
        inst,
        _obsdate_window(str(entry.get("tsdate") or "")),
        site,
        ra_deg,
        dec_deg,
        object_name=target,
    )
    if matching:
        result["exists"] = True
        result["exists_checked"] = True
        result["dataset_matched_object"] = matching.get("object", "")
        result["dataset_local_frames"] = int(matching.get("nframes", 0))
    else:
        result["exists"] = False
        result["exists_checked"] = True
    return result


def build_time_series_report(target: str, *, _now: float | None = None) -> dict:
    """Fetch ExoFOP time series for *target* and annotate db existence.

    Returns ``{"ok": True, "toi": <num>, "time_series": [...], "total": N}``
    (``total`` counts only rows with a resolvable observation, mirroring the
    archive page's convention of reporting dataset count alongside frames).
    """
    toi = resolve_toi_number(target)
    if toi is None:
        return {"ok": False, "error": "Target does not resolve to a known TOI"}
    entries = fetch_time_series(toi, _now=_now)
    rows = [check_time_series_exists(e, target=target) for e in entries]
    return {
        "ok": True,
        "toi": toi,
        "time_series": rows,
        "total": len(rows),
    }
