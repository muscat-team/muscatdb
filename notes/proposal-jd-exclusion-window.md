# Proposal: exclude a JD time range from data reduction

Status: **implemented**, per the phasing and decisions below. Rev 1's single
`--exclude_jd START,END` flag and GUI-textarea question are superseded by the
two-flag design decided in rev 2. Implementation notes from build-out:

- The frame filter had to move from the originally-proposed insertion point
  (`run_photometry.py:~5246`, alongside `--site`/`--mode`/`--telescope`) to
  the point where the muscat/muscat2 and BANZAI-instrument code paths
  *reconverge* (`run_photometry.py:~5486`, right after
  `date = date_from_header(probe)`). The earlier point would have silently
  no-op'd for muscat/muscat2: their `sciences` dict gets wholly replaced by a
  post-calibration glob later in `main()`, discarding any earlier filtering.
  See `_apply_jd_exclusion()` (`run_photometry.py:~950`).
- `header_jd()` reads via the existing `_read_quality_header()` (`.fits.fz`-
  tolerant), not bare `fits.getheader()` like `--site`/`--mode`/`--telescope`
  use -- BANZAI `.fits.fz` frames commonly carry their real header in HDU 1,
  and getting the JD wrong would silently mis-filter.
- The help text originally quoted `MAX_TIME_OFFSET_MIN` (16.8 min) directly
  for the boundary caveat; that constant is `compute_bjd_tdb`'s own 2x-margin
  sanity-check threshold, not the expected drift. Fixed to the correct ~8.4
  min (light travel time across 1 AU, i.e. `MAX_TIME_OFFSET_MIN / 2`).

## The ask

Let a user specify one or more JD time ranges to drop from a reduction before
it runs, for stretches of a night that are known-bad or useless (clouds,
guiding loss, dome-slit hiccup, satellite trail through the target, etc.) and
would otherwise pollute the light curve, the comparison-star choice, or the
reference frame.

## Where this lives: prose2, not muscat-db

Per `CLAUDE.md`: "do not duplicate functions between muscat-db and prose2.
all photometry functions should live in prose2." Frame selection (which FITS
files enter a reduction) is entirely a `run_photometry.py` concern today --
muscat-db's `photometry.py` only builds a CLI invocation and never touches a
FITS file itself. So the actual filtering logic belongs in
`ext_tools/prose2/prose/scripts/run_photometry.py`; muscat-db's job is CLI
passthrough + a GUI field, mirroring how `--site`/`--mode`/`--telescope`/
`--sig_bkg` etc. are already plumbed.

`prose2` is jpdeleon's own repo (this session's user), so this is a direct
edit there, not a fork-and-patch situation -- consistent with the `CLAUDE.md`
engine-ownership rule.

## Decisions

1. **Filter basis: raw header JD, before the full reduction runs** (not the
   final BJD_TDB, post-hoc). Same pipeline stage as `--site`/`--mode`/
   `--telescope`, before the expensive per-band `Sequence` runs -- see "How
   frame selection actually works today" below. Keeps excluded frames out of
   reference-frame/comparison-star selection too, not just the light curve.
   Tradeoff accepted: up to ~8 min boundary drift vs. the BJD_TDB values a
   user will actually be looking at on a light curve plot (see
   "The header-JD-vs-BJD_TDB tradeoff" below). **Not** auto-widened by a BJD
   margin -- use the user's stated JD values exactly as given.
2. **CLI shape: two flags, not one.**
   `--exclude_after_jd JD [JD ...]` and `--exclude_before_jd JD [JD ...]`,
   either or both, replacing rev 1's single `--exclude_jd START,END`. See
   "Proposed CLI shape" below for the full pairing/validation rules.
3. **`--refid` resolving inside an excluded stretch needs no new code.**
   Verified against `_find_frame_by_number` (`run_photometry.py:4405-4414`):
   it already searches only the *already-filtered* frame list and returns
   the closest surviving frame number, with no requirement that the exact
   requested number be present. Since the JD-exclusion filter runs at the
   same `sciences`-narrowing stage as `--site`/`--mode`/`--telescope` --
   before `--refid` resolution (`run_photometry.py:~5556`) ever runs -- an
   excluded frame is already gone from the candidate list by the time
   `--refid` is resolved. It is therefore automatically treated exactly like
   a genuinely missing FITS file (nearest-surviving-frame fallback), which
   is precisely what was asked for. No hard-error path needed.

## How frame selection actually works today (verified, not assumed)

`run_photometry.py`'s `main()` builds a `sciences: dict[band, list[path]]`
dict, then narrows it in a sequence of pre-reduction filters, each following
the same shape (`run_photometry.py:5090-5246`):

```python
if instrument in MULTISITE_INSTRUMENTS and args.site:
    filtered_sciences = {}
    for b, fs in sciences.items():
        matching = [f for f in fs if <header check via fits.getheader(f)>]
        if matching:
            filtered_sciences[b] = matching
    sciences = filtered_sciences
```

`--site`, `--mode`, `--telescope` each read `fits.getheader(f)` (header-only,
no pixel decode) to decide whether a frame stays. `--test_run` similarly
truncates `sciences` afterward (`run_photometry.py:5248-5258`). After every
filter stage, `active_bands` is recomputed and an empty result aborts with a
clear error. **This is the natural home for the new JD-exclusion filter** --
same shape, same place, right before `--test_run` truncation and well before
`--refid` resolution (`~5556`), which is what makes Decision 3 above hold.

## What "JD" should the filter compare against

Checked what's available *before* paying for a full reduction:

- `FITSImage()` (`prose/core/image.py:696-783`) is **not** cheap: it
  unconditionally does `fits.open()` + reads `hdu.data`, the full pixel
  array, regardless of its `load_data` parameter (that parameter is
  currently dead -- never checked in the body). Calling it once per candidate
  frame just to read `.jd` would reintroduce exactly the cost this filter is
  meant to avoid, and is why `--site`/`--mode`/`--telescope` use bare
  `fits.getheader()` instead.
- `prose/scripts/check_header_time.py` already has a header-only JD
  estimator built for exactly this purpose -- auditing prose's real resolved
  JD against ground truth:

  ```python
  def _truth_jd(header) -> tuple[float | None, str]:
      """Best-effort 'true' JD from the raw header, and the source keyword."""
      if "MJD-STRT" in header:
          return float(header["MJD-STRT"]) + 2_400_000.5, "MJD-STRT"
      if "MJD-OBS" in header:
          return float(header["MJD-OBS"]) + 2_400_000.5, "MJD-OBS"
      do = header.get("DATE-OBS")
      if do and ("T" in str(do) or ":" in str(do)):
          try:
              return float(Time(do).jd), "DATE-OBS"
          except Exception:
              return None, "DATE-OBS?"
      return None, "none"
  ```

  (`check_header_time.py:116-128`). It's currently private (`_truth_jd`) and
  lives in an audit script, but it is already used to validate agreement with
  the real per-frame JD prose computes during reduction
  (`_resolved_jd`, `check_header_time.py:131-140`), so its accuracy against
  prose's own pipeline is already established, not something this proposal
  has to re-derive or re-trust from scratch.

  `run_photometry.py` separately has its own `TIME_KEYS`/`MJD_TO_JD`-based
  date deriver (`_date_from_time_keys`, `run_photometry.py:468-489`), but it
  only returns a `YYMMDD` string for filenames, not a JD float -- not a fit
  for this.

**Proposal**: promote `_truth_jd` out of `check_header_time.py` into a
shared, public helper (e.g. `header_jd(header) -> float | None` in
`prose/utils.py`, alongside `get_saturation_from_header` and friends), used
by both `check_header_time.py` (unchanged behavior, just an import) and the
new exclusion filter in `run_photometry.py`. This avoids a third hand-rolled
"parse JD from a header" implementation and keeps the two in permanent
agreement by construction.

## The header-JD-vs-BJD_TDB tradeoff

The filter compares against a **header-derived, non-barycentric-corrected**
JD (effectively GJD_UTC). The light curve's actual time axis is
**BJD_TDB**, produced later by `compute_bjd_tdb` (`run_photometry.py:3079`)
after the full per-band `Sequence` has run. The two differ by up to the
light-travel-time correction, sanity-bounded in this codebase at
`MAX_TIME_OFFSET_MIN = 2 * 8.4` minutes (`run_photometry.py:317`).

In practice: a user will typically identify a bad stretch by eye on a
first-pass BJD_TDB light curve, then rerun with `--exclude_after_jd`/
`--exclude_before_jd` using those BJD_TDB values. Near the *boundary* of an
excluded window, up to ~8 minutes of frames could be included or excluded one
frame off from what they intended. Decided above: **not** auto-corrected --
document the caveat in the flags' help text and in the run log rather than
second-guess the user's stated values.

## Proposed CLI shape

```
--exclude_after_jd  JD [JD ...]
--exclude_before_jd JD [JD ...]
```

Either or both. Semantics, decided above ("positional pairing, validated"):

- **Both given**: the arrays must be the same length -- `ap.error(...)` if
  not (mirrors the existing `--aper_radii`/`--annulus` cross-argument
  validation already in `parse_args()`, `run_photometry.py:4972-4982`). The
  *i*-th `--exclude_after_jd` value pairs with the *i*-th
  `--exclude_before_jd` value to form one closed excluded chunk
  `(after[i], before[i])`. Each pair must satisfy `after[i] < before[i]` --
  `ap.error(...)` otherwise. N pairs -> N excluded chunks.
- **Only one given**: every one of its values becomes an independent
  open-ended cut with no matching bound -- `--exclude_after_jd A` alone
  excludes every frame with `JD > A` (no upper bound); `--exclude_before_jd
  B` alone excludes every frame with `JD < B` (no lower bound). Multiple
  unmatched values are allowed (e.g. `--exclude_after_jd A1 A2` alone is
  legal, if redundant -- the union collapses to `JD > min(A1, A2)`).

```
--exclude_after_jd  2460423.10 2460423.60
--exclude_before_jd 2460423.20 2460423.75
-> chunk 1: (2460423.10, 2460423.20)
-> chunk 2: (2460423.60, 2460423.75)

--exclude_after_jd 2460423.90   (alone)
-> excludes everything with JD > 2460423.90

--exclude_after_jd  2460423.10 2460423.60
--exclude_before_jd 2460423.20
-> ERROR: --exclude_after_jd and --exclude_before_jd must have the same
   number of values when both are given (2 vs 1)
```

Implementation shape:

- Cross-argument validation (length match, `after[i] < before[i]`) happens in
  `parse_args()`'s post-parse block, same place as the existing
  `--aper_radii`/`--annulus`/`--mode` checks (`run_photometry.py:4972-4994`).
  `type=float, nargs="+"` on each flag handles per-value parsing; no new
  `parse_*` helper needed (unlike rev 1's `parse_jd_range`, no longer
  applicable now that ranges aren't typed as single `"A,B"` tokens).
- The actual filtering happens in `main()`'s `sciences`-narrowing stage,
  right after the `--telescope`/`--mode` filters and before `--test_run`
  truncation (`run_photometry.py:~5246`), using the same `filtered_sciences`
  idiom as the existing filters, reading each candidate's header once via
  the new `header_jd()` helper. A frame is dropped if its JD falls inside
  any closed chunk, or fails any open-ended cut.
- Recompute `active_bands` afterward, same as today; if a band's frames are
  entirely excluded, it drops out through the existing "no frames" handling,
  not a new error path.
- Log a summary per band (`N frames excluded by --exclude_after_jd/
  --exclude_before_jd out of M`) at INFO level, consistent with existing
  filter-stage logging. Since `logger.info(f"args: {vars(args)}")` already
  runs at the top of `main()`, the applied cuts are automatically in every
  run's log for later audit -- no new sidecar file needed.

## muscat-db plumbing (CLI passthrough + GUI, no new logic)

Mirroring the existing `annulus`/`site`/`comparison_ids` pattern end to end:

1. **`RUN_DEFAULTS`** (`photometry.py:66-118`): add
   ```python
   "exclude_after_jd": "",   # "" -> none; comma-separated JD values
   "exclude_before_jd": "",  # "" -> none; comma-separated JD values
   ```
   Comma-separated, matching the existing `comparison_ids`/
   `avoid_comparison_ids` convention (`photometry.py:95-96`) rather than
   inventing a new list-input shape.
2. **`normalize_run_options`** (`photometry.py:1120+`): pass-through string
   fields, same tier as `comparison_ids` -- no client-side pairing/length
   validation. A length mismatch or bad pairing is caught by
   `run_photometry.py`'s own `argparse` post-parse validation and surfaces
   as a failed job with the error in the log, same as today's bad
   `--annulus` input.
3. **argv builder** (`photometry.py:~1300`, next to the `annulus`/`site`
   block): split each stored string on `,` into float tokens and emit
   `--exclude_after_jd tok1 tok2 ...` / `--exclude_before_jd tok1 tok2 ...`
   only when non-empty (same pattern as the existing `comparison_ids` ->
   `--cID` handling at `photometry.py:1321-1324`).
4. **`templates/photometry.html`**: two text inputs (not a textarea -- these
   are comma-separated value lists like `comparison_ids`, not multi-line
   ranges) near the `site`/`telescope`/`mode` fields, with a help tooltip
   documenting the pairing rule and the header-JD-vs-BJD_TDB caveat above.
   Per `CLAUDE.md`, register both new field ids in `collectOptions`,
   `restoreOptions`, and the default-settings listener arrays
   (`photometry.html:892`, `969`, and the change-listener block) so they
   persist across page navigation like every other option.

No new validation logic gets duplicated in muscat-db -- the pairing rules,
length check, and all semantics stay in `run_photometry.py`'s
`parse_args()`.

## Suggested phasing

1. `prose2`: promote `_truth_jd` to `header_jd()` in `prose/utils.py`
   (behavior-preserving refactor of `check_header_time.py`).
2. `prose2`: add `--exclude_after_jd`/`--exclude_before_jd`, the
   cross-argument validation in `parse_args()`, and the `sciences`
   pre-filter stage in `main()`; log summary.
3. Manual smoke test on the host: rerun a known dataset with an excluded
   stretch covering real bad frames, confirm frame counts and light curve
   match expectations, confirm the excluded frames' absence from the
   `*_ref.png`/`*_apertures.png`/comparison-star diagnostics, and confirm an
   explicit `--refid` pointing inside the excluded stretch falls back to the
   nearest surviving frame as expected (Decision 3).
4. `muscat-db`: `RUN_DEFAULTS` + `normalize_run_options` + argv builder +
   `templates/photometry.html` (two fields + JS registration), once step 3
   has confirmed the CLI contract is stable.
