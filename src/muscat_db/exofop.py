"""ExoFOP time-series cross-checking for the LCO archive page.

ExoFOP (https://exofop.ipac.caltech.edu) records ground-based ``time_series``
entries (typically TFOPWG transit follow-up reports) against a TESS/TOI target.
Each entry describes one reported observing run: telescope, camera, filter, the
UT date observed, the number of frames, and a ``tstag``.

``tstag`` is *not* an LCO request id, despite the name resembling one -- per
ExoFOP's own documentation it is ExoFOP's internal Data Tag (format
``YYYYMMDD_username_description_number``, shortened to an integer), used to
link an observer's own uploads together. It carries no relationship to LCO's
request/observation numbering. Verified against the live archive for both a
2020 and a 2024 report: ``request_id=<tstag>`` resolves to zero frames every
time. The download pathway therefore searches the archive by target
coordinates + a date window around the entry's reported UT date (the same
"Search LCO Archive" tab search), not by ``tstag``.

This module lets the LCO archive page answer "which reported ExoFOP time series
for this target do we already have in muscat-db, and which are missing?", so an
operator can fetch the ones we lack from the LCO archive.

The "already in the database" test is purely local (against the muscat-db frames
table) so the report makes no extra LCO archive API calls; only the download
pathway (``archive_search_by_target_date``) queries the LCO archive.
"""

from __future__ import annotations

import datetime
import re
import threading
from urllib.parse import quote

from muscat_db import catalog, lco

# How recently a fetched ExoFOP response is considered fresh enough to reuse
# (seconds). ExoFOP time series change slowly, so a short TTL lets an operator
# re-run a search in the same session without hammering the external service.
_EXOFOP_CACHE_TTL_S = 3600
_EXOFOP_FAILURE_CACHE_TTL_S = 30

_exofop_cache: dict = {}
_exofop_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# TOI number resolution
# --------------------------------------------------------------------------- #

_TOI_NAME_RE = re.compile(r"^TOI0*(\d+)(?:\.\d+)?$", re.IGNORECASE)
_PLAIN_TOI_RE = re.compile(r"^toi0*(\d+)$", re.IGNORECASE)
_TIC_NAME_RE = re.compile(r"^TIC[\s_-]*0*(\d+)$", re.IGNORECASE)


def resolve_tic_number(target: str) -> str | None:
    """Return the numeric TIC id for a ``TIC434398831`` / ``TIC-434398831`` name."""
    m = _TIC_NAME_RE.match((target or "").strip())
    return m.group(1) if m else None


def normalize_tic_input(value: str | int | None) -> str | None:
    """Return the digits of a user-supplied TIC id (``"67522"``, ``"TIC 67522"``).

    None when *value* is empty or not a plain TIC id.
    """
    text = str(value or "").strip()
    if not text:
        return None
    m = re.match(r"^(?:TIC[\s_-]*)?0*(\d+)$", text, re.IGNORECASE)
    return m.group(1) if m else None


_SIMBAD_TIC_CACHE: dict[str, str | None] = {}
_SIMBAD_TIC_RE = re.compile(r"^TIC\s+0*(\d+)$", re.IGNORECASE)


def resolve_tic_via_simbad(target: str) -> str | None:
    """Return the TIC id SIMBAD lists among the aliases of *target*, or None.

    Covers stellar names that are neither a TOI nor a ``TIC`` designation
    (``HIP 67522``). Best effort: any network/astroquery failure yields None so
    the caller can ask the user for the TIC id instead.
    """
    name = (target or "").strip()
    if not name:
        return None
    key = name.upper()
    if key in _SIMBAD_TIC_CACHE:
        return _SIMBAD_TIC_CACHE[key]
    tic = None
    try:
        from astroquery.simbad import Simbad

        table = Simbad.query_objectids(name)
        for row in table or []:
            ident = str(row[0]).strip()
            m = _SIMBAD_TIC_RE.match(" ".join(ident.split()))
            if m:
                tic = m.group(1)
                break
    except Exception:
        tic = None
    if tic is not None:  # don't cache a failure: it may be a transient outage
        _SIMBAD_TIC_CACHE[key] = tic
    return tic


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

def _exofop_url(toi: str = "", tic: str = "") -> str:
    # ExoFOP accepts either ``id=TOI-<n>`` or a bare TIC id.
    ident = f"TOI-{toi}" if toi else str(tic)
    return f"https://exofop.ipac.caltech.edu/tess/target.php?id={ident}&json"


class ExofopLookupError(RuntimeError):
    """The report is unavailable, rather than a successful empty result."""


def fetch_time_series(
    toi: str, *, tic: str = "", _now: float | None = None
) -> list[dict]:
    """Return the ExoFOP ``time_series`` list, cached.

    Keyed by the TOI host number; *tic* is the fallback id for a target that
    is not a known TOI.
    """
    toi = str(toi or "").strip()
    tic = str(tic or "").strip()
    if not toi and not tic:
        return []
    cache_key = f"ts:{toi}" if toi else f"ts:tic:{tic}"
    now = _now if _now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    with _exofop_cache_lock:
        hit = _exofop_cache.get(cache_key)
        if hit is not None and now - hit[0] < _EXOFOP_CACHE_TTL_S:
            return [dict(r) for r in hit[1]]

    try:
        resp = catalog._sync_get(
            _exofop_url(toi, tic),
            headers={"User-Agent": "MuSCAT-db/0.1.0"},
            timeout=20,
        )
        data = resp.json()
        entries = data.get("time_series") or []
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError("Invalid ExoFOP time_series response")
        entries = [dict(e) for e in entries]
    except Exception as exc:
        raise ExofopLookupError("ExoFOP lookup failed. Please try again.") from exc

    with _exofop_cache_lock:
        _exofop_cache[cache_key] = (now, [dict(e) for e in entries])
    return entries


def fetch_target_by_name(name: str, *, _now: float | None = None) -> dict | None:
    """Resolve a TESS target through ExoFOP, retaining coordinates and TIC ID.

    Share the response with time-series callers. Cache failed lookups too so
    repeated clicks during an outage do not keep hitting ExoFOP. Failures expire
    after 30 seconds so transient errors do not suppress recovery for an hour.
    """
    from copy import deepcopy

    ident = (name or "").strip()
    if not ident:
        return None
    cache_key = f"target:{ident.upper()}"
    now = _now if _now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    with _exofop_cache_lock:
        hit = _exofop_cache.get(cache_key)
        if hit is not None:
            ttl = _EXOFOP_FAILURE_CACHE_TTL_S if hit[1] is None else _EXOFOP_CACHE_TTL_S
            if now - hit[0] < ttl:
                return deepcopy(hit[1])
    data = None
    try:
        resp = catalog._sync_get(
            f"https://exofop.ipac.caltech.edu/tess/target.php?id={quote(ident)}&json",
            headers={"User-Agent": "MuSCAT-db/0.1.0"},
            timeout=5,
        )
        candidate = resp.json()
        info = candidate.get("basic_info") if isinstance(candidate, dict) else None
        if isinstance(info, dict) and normalize_tic_input(info.get("tic_id")) is not None:
            data = candidate
    except Exception:
        pass
    with _exofop_cache_lock:
        # Bound cache memory even for arbitrary user-supplied target names.
        if len(_exofop_cache) >= 256:
            _exofop_cache.pop(next(iter(_exofop_cache)))
        _exofop_cache[cache_key] = (now, deepcopy(data))
    return data


def fetch_by_name(name: str, *, _now: float | None = None) -> tuple[str, list[dict]] | None:
    """Return the resolved TIC ID and reported time series, cached by name."""
    data = fetch_target_by_name(name, _now=_now)
    if data is None:
        return None
    tic = normalize_tic_input(data["basic_info"]["tic_id"])
    return tic, [dict(e) for e in (data.get("time_series") or [])]


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
    # "TEID" also covers ExoFOP's truncated "LCO-Teid-1m0" / "LCO-TEID (1.0 m)".
    "TEID": "tfn",
    "TLV": "tlv",
    "WISE": "tlv",
}

# Known 1 m / 0.4 m cameras in ExoFOP follow-up reports -> LCO-style INSTRUME.
_CAMERA_TO_INSTRUME = {
    "SINISTRO": "sinistro",
    "MUSCAT": "muscat",
    "QHY600": "qhy600",
    "SBIG": "sbig",
    "STL": "sbig",
}


# "MuSCAT2", "MuSCAT 3", "muscat-4", "MUSCAT_3" -- the digit names the instrument.
_MUSCAT_NUMBERED_RE = re.compile(r"MU?SCAT[\s_-]*([234])(?!\d)", re.IGNORECASE)


def _site_from_tel(tstel: str) -> str:
    upper = (tstel or "").upper()
    for token, code in _SITE_TOKENS.items():
        if token in upper:
            return code
    # ExoFOP abbreviates Haleakala as a standalone "HAL" (e.g. "LCO-HAL-M3 (2.0 m)",
    # "LCO-HAL-2m0"); whole-token only, so it cannot match inside another word.
    if re.search(r"(?<![A-Z])HAL(?![A-Z])", upper):
        return "ogg"
    # SSO is ExoFOP's abbreviation for Siding Spring (LCO site COJ).
    # Match a whole token so unrelated observatory names do not become LCO.
    if re.search(r"(?<![A-Z])SSO(?![A-Z])", upper):
        return "coj"
    # Match the uppercase input, then return canonical lowercase site codes.
    m = re.search(r"\b(CPT|LSC|OGG|COJ|ELP|TFN|TLV)\b", upper)
    return m.group(1).lower() if m else ""


def is_lco_telescope(tstel: str) -> bool:
    """True when an ExoFOP ``tstel`` string names an LCO telescope."""
    # Other facilities share LCO observatory locations (e.g. iTelescope at
    # Siding Spring); a site name alone does not identify an LCO telescope.
    return bool(re.search(r"\bLCO(?:GT)?\b", (tstel or "").upper()))


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
    # An explicitly numbered MuSCAT camera names its instrument outright
    # ("MuSCAT2", "MuSCAT 3", "muscat-4"); only the bare "MuSCAT" form needs
    # the site/telescope inference below.
    m = _MUSCAT_NUMBERED_RE.search(camera)
    if m:
        return f"muscat{m.group(1)}"
    instrume = ""
    for token, kind in _CAMERA_TO_INSTRUME.items():
        if token in camera.upper():
            instrume = kind
            break
    if not instrume and re.search(r"\bFA\d*\b", camera, re.IGNORECASE):
        # Sinistro camera-unit codes (fa11, fa15, ...).
        instrume = "sinistro"
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
# Archive search for one ExoFOP time-series entry (by target + date, not tag)
# --------------------------------------------------------------------------- #

def _filter_to_telescope(rows: list[dict], site: str, instrument: str) -> list[dict]:
    """Keep only frames from *site* and of muscat-db *instrument* (when given)."""
    site = (site or "").lower()
    instrument = (instrument or "").lower()
    if not site and not instrument:
        return rows
    kept = []
    for r in rows:
        if site and str(r.get("site_id") or r.get("SITEID") or "").lower() != site:
            continue
        if instrument:
            try:
                if lco.infer_archive_instrument(r).lower() != instrument:
                    continue
            except lco.LcoError:
                continue
        kept.append(r)
    return kept


def archive_search_by_target_date(
    target: str,
    tsdate: str,
    *,
    reduction_level: str = "91",
    user_name: str | None = None,
    site: str = "",
    instrument: str = "",
) -> list[dict]:
    """Search the LCO archive for the observation behind one time-series entry.

    ``site`` / ``instrument`` (the entry's inferred LCO site code and
    muscat-db instrument) narrow the result to that entry's own telescope;
    without them every LCO frame covering the target that night is returned,
    including other telescopes' (e.g. 0.4 m qhy600 frames beside a 1 m
    Sinistro report).

    ``tstag`` is not usable for this: it is ExoFOP's own internal Data Tag,
    not an LCO request id (see the module docstring), and confirmed against
    the live archive to resolve to zero frames for both a 2020 and a 2024
    report. So instead of ``request_id=<tstag>``, this runs the same
    coordinate + date-window search the "Search LCO Archive" tab uses: the
    target's resolved coordinates via ``covers=POINT(...)``, bounded to +/-1
    day around the entry's reported UT date (matching ``_obsdate_window``'s
    existing tolerance rationale for the same reason -- the observing-night
    label can lag the UT date by a day at sites whose night crosses local
    midnight). Returns ``[]`` (never raises) when the target's coordinates
    can't be resolved, ``tsdate`` can't be parsed, or the archive call itself
    fails, so callers can treat this uniformly as "search, then report
    not-found" without extra error handling.
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
                # Exclude auto-focus/engineering frames the telescope took
                # near the same footprint (real example: TOI-1807 turns up
                # "auto_focus" frames at the same reduction level as its
                # actual science data). See the matching comment on the
                # "Search LCO Archive" tab's own coordinate search.
                "OBSTYPE": "EXPOSE",
            },
            user_name,
        )
    except lco.LcoError:
        return []
    rows = result.get("results") or []
    rows = _filter_to_telescope(rows, site, instrument)
    # OBSTYPE=EXPOSE (above) doesn't catch an engineering frame that was
    # itself submitted as a plain EXPOSE block (observed: an auto-focus
    # sequence filed with a real "e91" filename) -- this is the narrower,
    # OBJECT-based net for that specific case.
    return [r for r in rows if not lco.is_engineering_object(r.get("OBJECT") or "")]


# --------------------------------------------------------------------------- #
# Existence cross-check against muscat-db
# --------------------------------------------------------------------------- #

def check_time_series_exists(
    entry: dict, *, target: str = "", denied: frozenset[str] = frozenset()
) -> dict:
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
    # Only LCO telescopes can be fetched from the LCO archive; the UI uses this
    # to decide whether to offer a Download button.
    result["lco"] = is_lco_telescope(str(entry.get("tstel") or ""))

    if not inst or not site:
        result["exists"] = False
        result["exists_checked"] = False
        result["exists_unchecked_reason"] = (
            "Instrument could not be identified." if not inst
            else "Observatory site could not be identified."
        )
        return result

    coords = _target_coords(target) if target else None
    if coords is None:
        result["exists"] = False
        result["exists_checked"] = False
        result["exists_unchecked_reason"] = "Target coordinates could not be resolved."
        return result
    ra_deg, dec_deg = coords

    matching = lco.local_lco_dataset_match(
        inst,
        _obsdate_window(str(entry.get("tsdate") or "")),
        site,
        ra_deg,
        dec_deg,
        object_name=target,
        denied=denied,
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


def build_time_series_report(
    target: str,
    *,
    tic: str | None = None,
    _now: float | None = None,
    denied: frozenset[str] = frozenset(),
) -> dict:
    """Fetch ExoFOP time series for *target* and annotate db existence.

    Returns ``{"ok": True, "toi": <num>, "time_series": [...], "total": N}``
    (``total`` counts only rows with a resolvable observation, mirroring the
    archive page's convention of reporting dataset count alongside frames).
    """
    toi = None
    entries: list[dict] | None = None
    if tic and normalize_tic_input(tic) is None:
        return {"ok": False, "error": f"Invalid TIC id: {tic!r}", "needs_tic": True}
    tic = normalize_tic_input(tic)
    if tic is None:
        # Name -> TOI, then TIC designation, then SIMBAD aliases (HIP, HD, ...).
        toi = resolve_toi_number(target)
        if toi is None:
            tic = resolve_tic_number(target)
        if toi is None and tic is None:
            # Not a TOI/TIC name: let ExoFOP resolve the name itself (HIP, HD,
            # Gaia, ...), then fall back to SIMBAD's TIC alias.
            by_name = fetch_by_name(target, _now=_now)
            if by_name is not None:
                tic, entries = by_name
            else:
                tic = resolve_tic_via_simbad(target)
            if tic is None:
                return {
                    "ok": False,
                    "needs_tic": True,
                    "error": (
                        "Target does not resolve to a known TOI or TIC id. "
                        "Enter its TIC id to check ExoFOP."
                    ),
                }
    if entries is None:
        try:
            entries = fetch_time_series(toi or "", tic=tic or "", _now=_now)
        except ExofopLookupError as exc:
            return {"ok": False, "error": str(exc)}
    rows = [check_time_series_exists(e, target=target, denied=denied) for e in entries]
    return {
        "ok": True,
        "toi": toi,
        "tic": tic,
        "time_series": rows,
        "total": len(rows),
    }
