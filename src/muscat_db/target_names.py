"""Target-name normalization, pure and dependency-free.

Moved out of :mod:`muscat_db.catalog` so modules below it in the import graph
(:mod:`muscat_db.database`, :mod:`muscat_db.target_keys`) can key target names
without importing the web-facing catalog module. ``catalog`` re-exports every
name here, so existing imports keep working.
"""

from __future__ import annotations

import re

# MuSCAT OBJECT values can carry an observing-date and coordinate decoration
# after an otherwise valid TOI name, e.g.
# ``TOI-179b_231015 J025710-560913``.  Match the complete known decoration so
# legitimate underscores such as the one in ``TOI_2457`` are never truncated.
_DECORATED_TOI_OBJECT_RE = re.compile(
    r"^(?P<target>TOI(?:[ _-]*)0*\d+(?:\.\d+|[B-H])?)_"
    r"\d{6}\s+J\d{6}[+-]\d{6}$",
    re.IGNORECASE,
)


# Real star names that happen to end in a letter the trailing-planet-letter
# heuristic below would otherwise strip -- e.g. "AU Mic" (AU Microscopii):
# "MIC" is the constellation abbreviation, not a planet letter, but stripping
# rule has no way to tell that apart from "V1298Tauc" (a real planet-letter
# suffix glued directly onto a name with no separator, which the same rule
# must keep stripping -- see the V1298Tauc case asserted in
# tests/test_photometry.py). A per-object target_overrides row only fixes
# the exact raw spelling it was set on, so a second raw spelling of the same
# star (e.g. "AUMic" vs "AU Mic") silently mis-normalizes again; listing the
# canonical (already letter/punctuation-stripped, uppercased) name here fixes
# every raw spelling at once.
_LETTER_SUFFIX_EXCEPTIONS = {"AUMIC"}


# LCO key-project OBJECT/target names decorate a primary designation with a
# parenthesized alias, e.g. ``TIC245728942.01(TOI5012.01)`` -- the TIC-based
# candidate label LCO assigns, with the TOI committee's own designation
# appended for cross-reference. Treated as one token, the leading TIC digit
# run defeats every TOI-number regex below it (it parses as host 245728942,
# candidate .01, rather than TOI 5012.01), which is what silently sent every
# TOI-catalog and SIMBAD lookup for this target to the wrong place.
_LCO_KEY_PROJECT_ALIAS_RE = re.compile(r"^(?P<primary>[^()]+?)\s*\((?P<alias>[^()]+)\)$")
_TOI_SHAPE_RE = re.compile(r"TOI[ _-]*0*\d+(?:\.\d+)?(?:\s*[B-H])?", re.IGNORECASE)


def resolve_lco_key_project_name(raw: str) -> str:
    """Resolve an LCO key-project decorated target name to the single
    designation worth querying a catalog or SIMBAD with.

    Splits a ``PRIMARY(ALIAS)`` name into its two halves and returns whichever
    is a recognizable TOI designation -- catalogs and SIMBAD are keyed on the
    TOI number, not LCO's TIC-based candidate label -- preferring the alias
    since that is where LCO puts it in every observed key-project name.
    Falls back to the primary half when neither is TOI-shaped, and to *raw*
    unchanged (just stripped) when there is no parenthetical to split at all,
    so this is a no-op for every already-plain target name.
    """
    raw = (raw or "").strip()
    m = _LCO_KEY_PROJECT_ALIAS_RE.fullmatch(raw)
    if not m:
        return raw
    primary, alias = m.group("primary").strip(), m.group("alias").strip()
    for part in (alias, primary):
        if _TOI_SHAPE_RE.fullmatch(part):
            return part
    return primary


# Helper to normalize target names for comparison
def _normalize_target_name(t: str, overrides: dict[str, str] | None = None) -> str:
    # Check user override first
    if overrides and t in overrides:
        return overrides[t]
    # Resolve an LCO key-project "TIC...(TOI...)" decoration before anything
    # else, so the TOI-shape checks below see the actual designation instead
    # of an undigestible compound string. No-op for a plain name.
    t = resolve_lco_key_project_name(t)
    # Parse recognized TOI spellings before removing punctuation.  Otherwise a
    # malformed value such as ``TOI06209-01`` would be reinterpreted as TOI
    # 620901 after the hyphen is discarded.  TOI comparison keys represent the
    # host, so candidate-number and confirmed-planet suffixes are omitted.
    raw = t.strip().upper()
    decorated_toi = _DECORATED_TOI_OBJECT_RE.fullmatch(raw)
    if decorated_toi:
        raw = decorated_toi.group("target")
    toi_match = re.fullmatch(
        r"TOI(?:[ _-]*)0*(\d+)(?:\.\d+)?(?:\s*[B-H])?",
        raw,
    )
    if toi_match:
        return f"TOI{int(toi_match.group(1))}"

    s = raw.replace(" ", "").replace("-", "").replace("_", "")
    s = re.sub(r"\.\d+$", "", s)
    if s in _LETTER_SUFFIX_EXCEPTIONS:
        return s
    if len(s) > 2 and s[-1] in "BCDEFGH":
        return s[:-1]
    return s
