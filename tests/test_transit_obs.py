"""Unit tests for transit observability (muscat_db/transit_obs.py).

Uses real astropy with fixed times (no Time.now), so results are deterministic.
"""

from __future__ import annotations


import pytest

from muscat_db import transit_obs as T


# --------------------------------------------------------------------------- #
# constraint helpers
# --------------------------------------------------------------------------- #


def test_alt_limit_from_airmass():
    assert T.alt_limit_from_airmass(2.0) == pytest.approx(30.0, abs=1e-6)
    assert T.alt_limit_from_airmass(1.6) == pytest.approx(38.68, abs=0.1)
    assert T.alt_limit_from_airmass(1.0) == pytest.approx(90.0, abs=1e-6)


def test_alt_limit_rejects_bad_airmass():
    with pytest.raises(T.TransitObsError):
        T.alt_limit_from_airmass(0.5)


def test_twilight_limit_mapping_and_validation():
    assert T.twilight_limit("civil") == -6.0
    assert T.twilight_limit("nautical") == -12.0
    assert T.twilight_limit("astronomical") == -18.0
    assert T.twilight_limit(None) == -12.0  # default nautical
    with pytest.raises(T.TransitObsError):
        T.twilight_limit("dusk")


def test_sites_for_kind():
    assert T.sites_for_kind("muscat") == ["ogg", "coj"]
    assert set(T.sites_for_kind("sinistro")) == {"lsc", "cpt", "coj", "tfn", "elp"}
    with pytest.raises(T.TransitObsError):
        T.sites_for_kind("hubble")


def test_resolve_site_list_priority():
    # Explicit sites win, normalised and de-duplicated.
    assert T.resolve_site_list(["COJ", "ogg", "coj"]) == ["coj", "ogg"]
    # Falls back to the kind when no sites are given.
    assert T.resolve_site_list(None, "muscat4") == ["coj"]
    # Empty/omitted with no kind => the full LCO network.
    assert T.resolve_site_list() == list(T.LCO_SITES)
    assert T.resolve_site_list([], None) == list(T.LCO_SITES)


def test_resolve_site_list_rejects_unknown_site():
    with pytest.raises(T.TransitObsError):
        T.resolve_site_list(["ogg", "atlantis"])


# --------------------------------------------------------------------------- #
# classify_transits
# --------------------------------------------------------------------------- #


def _windows(n=6, start_hour=4):
    return [{"epoch": i, "mid": f"2026-03-15T{start_hour + i:02d}:30:00"} for i in range(n)]


def test_classify_returns_one_aligned_entry_per_window():
    wins = _windows(5)
    res = T.classify_transits(97.64, 29.67, wins, "muscat", 2.5)
    assert len(res) == len(wins)
    for r in res:
        assert r["rating"] in ("full", "split_gap", "split_overlap", "partial", "none")
        assert set(r["sites"]).issubset({"ogg", "coj"})
        if r["rating"] == "none":
            assert r["best_site"] is None
        else:
            assert r["best_site"] in r["sites"]
        if r["rating"] == "full":
            assert r["best_site"] in r["sites"]


def test_classify_moon_phase_cap_forces_none():
    # max_lunar_phase=0 admits only a perfectly new (0% illuminated) Moon, which
    # never holds across a real window, so every window is rejected. (A 180 deg
    # min-separation no longer forces none: it is skipped whenever the Moon is
    # below the horizon or < 10% illuminated.)
    res = T.classify_transits(97.64, 29.67, _windows(4), "muscat", 2.5, max_lunar_phase=0.0)
    assert all(r["rating"] == "none" for r in res)


def test_observable_mask_separation_cut_and_phase_cap():
    from astropy.time import Time
    from astropy.coordinates import SkyCoord, get_body

    loc = T._earth_location("lsc")
    # Full-moon night, Moon well above the horizon at LSC; target sits on the Moon.
    t = Time(["2024-01-25T04:00:00"])
    moon = get_body("moon", Time("2024-01-25T04:00:00"))
    tgt = SkyCoord(moon.ra, moon.dec)

    mask, _talt, malt, _salt, msep, illum = T._observable_mask(
        tgt, loc, t, 20.0, -12.0, moon_sep_min=30.0)
    assert malt[0] > 0 and illum[0] > 0.9 and msep[0] < 5.0  # Moon up, bright, on-target
    assert not bool(mask[0])  # rejected by the min-separation cut

    # No separation cut, but the phase cap drops the bright Moon.
    mask_phase, *_ = T._observable_mask(
        tgt, loc, t, 20.0, -12.0, moon_sep_min=0.0, max_lunar_phase=0.5)
    assert not bool(mask_phase[0])


def test_classify_is_monotonic_in_strictness():
    wins = _windows(8)
    lenient = T.classify_transits(97.64, 29.67, wins, "sinistro", 2.5,
                                  max_airmass=40, twilight="civil", moon_sep_min=0)
    strict = T.classify_transits(97.64, 29.67, wins, "sinistro", 2.5,
                                 max_airmass=1.1, twilight="astronomical", moon_sep_min=120)
    n_lenient = sum(r["rating"] != "none" for r in lenient)
    n_strict = sum(r["rating"] != "none" for r in strict)
    assert n_lenient >= n_strict


def test_classify_empty_windows():
    assert T.classify_transits(97.64, 29.67, [], "muscat", 2.5) == []


def test_classify_explicit_sites_override_kind():
    # Passing ``sites`` restricts the evaluation regardless of ``kind``.
    wins = _windows(6)
    res = T.classify_transits(97.64, 29.67, wins, "muscat", 2.5, sites=["lsc"])
    for r in res:
        assert set(r["sites"]).issubset({"lsc"})


def test_classify_full_network_default_when_no_sites_or_kind():
    # kind=None and no sites => the full LCO network is considered, so reported
    # sites can include codes outside any single instrument kind.
    wins = _windows(8, start_hour=0)
    res = T.classify_transits(97.64, 29.67, wins, None, 2.5, max_airmass=40,
                              twilight="civil", moon_sep_min=0, sites=None)
    seen = set()
    for r in res:
        seen.update(r["sites"])
    assert seen.issubset(set(T.LCO_SITES))


def test_classify_checks_padding_observability():
    # A window that is rated "full" on the bare transit:
    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res_no_pad = T.classify_transits(97.64, 29.67, wins, "muscat", 2.0)
    assert res_no_pad[0]["rating"] == "full"

    # Add a start time in the middle of the day (20 UTC / 9:30 AM local), when
    # the sun is up, making the padded baseline unobservable.
    wins_with_bad_pad = [{
        "epoch": 0,
        "start": "2026-03-15T20:00:00",
        "mid": "2026-03-15T07:30:00",
        "end": "2026-03-15T08:30:00",
    }]
    # Default: padding is span-only metadata, so the bare transit still rates "full".
    res_default = T.classify_transits(97.64, 29.67, wins_with_bad_pad, "muscat", 2.0)
    assert res_default[0]["rating"] == "full"

    # include_padding=True: the unobservable baseline demotes it below "full".
    res_with_bad_pad = T.classify_transits(
        97.64, 29.67, wins_with_bad_pad, "muscat", 2.0, include_padding=True
    )
    assert res_with_bad_pad[0]["rating"] != "full"


# --------------------------------------------------------------------------- #
# split rating (two-site relay)
#
# These use ``monkeypatch`` on ``_earth_location``/``_observable_mask`` so the
# per-site coverage pattern is deterministic and independent of real
# astronomical geometry -- what's under test is the pair-search/rating logic
# in ``classify_transits``, not the astropy math (already covered above).
# --------------------------------------------------------------------------- #


def _patch_site_masks(monkeypatch, site_true_ranges):
    """Route ``_observable_mask`` to a per-site boolean pattern keyed by site
    name. ``site_true_ranges`` maps site -> (start_idx, end_idx) marked True
    (end exclusive); sites not present are all-False (no coverage)."""

    import numpy as np

    monkeypatch.setattr(T, "_earth_location", lambda site: site)

    def fake_observable_mask(target, location, times, alt_min, sun_alt_max,
                             moon_sep_min, max_lunar_phase=1.0):
        n = len(times)
        mask = np.zeros(n, dtype=bool)
        rng = site_true_ranges.get(location)
        if rng:
            mask[rng[0]:rng[1]] = True
        zeros = np.zeros(n)
        return mask, zeros, zeros, zeros, zeros, zeros

    monkeypatch.setattr(T, "_observable_mask", fake_observable_mask)


def _n_samp_for(duration_hours):
    return max(5, int(round(duration_hours * 60.0 / T._CLASSIFY_STEP_MIN)) + 1)


def test_classify_split_overlap_when_two_sites_meet_cleanly(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)  # 41 samples for a 2h transit at 3-min cadence
    mid = n // 2
    # lsc covers the early ~55%, cpt the late ~55% -- overlapping in the middle,
    # together spanning the whole transit with no gap.
    _patch_site_masks(monkeypatch, {"lsc": (0, mid + 2), "cpt": (mid - 2, n)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])

    assert res[0]["rating"] == "split_overlap"
    assert res[0]["split_sites"] == ["lsc", "cpt"]  # chronological: lsc starts first
    assert res[0]["split_gap_min"] == 0.0
    assert res[0]["split_overlap_min"] > 0.0
    sites_by_name = {w["site"]: w for w in res[0]["split_windows"]}
    assert set(sites_by_name) == {"lsc", "cpt"}
    assert not sites_by_name["lsc"]["fragmented"]
    assert not sites_by_name["cpt"]["fragmented"]


def test_classify_split_gap_when_relay_leaves_a_gap(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # lsc covers only the first third, cpt only the last third: a real gap
    # remains in the middle even combined -- this is a relay (2+ sites with
    # coverage), so it must resolve to "split_gap", not a vague "partial".
    _patch_site_masks(monkeypatch, {"lsc": (0, n // 3), "cpt": (2 * n // 3, n)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt"])

    assert res[0]["rating"] == "split_gap"
    assert res[0]["split_sites"] == ["lsc", "cpt"]
    assert res[0]["split_gap_min"] > 0.0
    assert res[0]["split_overlap_min"] == 0.0


def test_classify_split_falls_back_to_partial_when_union_misses_a_contact(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # cpt sees only a middle sliver (no contact); lsc sees the back half incl.
    # egress (the last sample). The pair's union covers egress but NOT ingress
    # (sample 0), so it is not a genuine relay -- it falls back to a partial on
    # the useful single site (lsc), not a misleading cpt->lsc split.
    _patch_site_masks(monkeypatch, {"cpt": (n // 2 - 3, n // 2), "lsc": (n // 2, n)})
    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])
    assert res[0]["rating"] == "partial"
    assert res[0]["best_site"] == "lsc"
    assert "split_sites" not in res[0]


def test_classify_split_falls_back_to_none_when_union_misses_both_contacts(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # Two disjoint middle slivers, neither touching ingress (sample 0) nor
    # egress (last sample): no contact anywhere, so the transit is "none".
    _patch_site_masks(monkeypatch, {"cpt": (n // 3, n // 3 + 3), "lsc": (2 * n // 3, 2 * n // 3 + 3)})
    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])
    assert res[0]["rating"] == "none"
    assert res[0]["best_site"] is None
    assert res[0]["sites"] == []


def test_classify_partial_reserved_for_single_site(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # Only lsc has coverage, and it spans the front half -- so it includes the
    # ingress contact (sample 0). One site + a contact observed => "partial".
    _patch_site_masks(monkeypatch, {"lsc": (0, n // 2)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])

    assert res[0]["rating"] == "partial"
    assert res[0]["sites"] == ["lsc"]
    assert res[0]["best_site"] == "lsc"
    assert "split_sites" not in res[0]


def test_classify_partial_requires_a_contact_not_mid_only(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # The single covering site sees only the middle of the transit -- neither
    # ingress (sample 0) nor egress (last sample). With no contact observed this
    # is not a useful partial and is rated "none".
    _patch_site_masks(monkeypatch, {"lsc": (n // 2 - 5, n // 2 + 6)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])

    assert res[0]["rating"] == "none"
    assert res[0]["sites"] == []
    assert res[0]["best_site"] is None


def test_classify_partial_on_egress_contact(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # A single site covering the back half sees egress (the last sample): a
    # genuine partial even though ingress is missed.
    _patch_site_masks(monkeypatch, {"lsc": (n // 2, n)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])

    assert res[0]["rating"] == "partial"
    assert res[0]["best_site"] == "lsc"


def test_classify_full_site_skips_pair_search(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # ogg covers the whole transit alone; coj covers only part of it. A valid
    # split pair may exist, but a full single site is always preferred.
    _patch_site_masks(monkeypatch, {"ogg": (0, n), "coj": (0, n // 2)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["ogg", "coj"])

    assert res[0]["rating"] == "full"
    assert res[0]["best_site"] == "ogg"
    assert "split_sites" not in res[0]


def test_classify_ingress_bracket_flags(monkeypatch):
    # duration=2h, pad 30 min each side -> the padded grid spans 3h
    # (06:00-09:00). At 3-min cadence that's n=61 samples; ingress (06:30)
    # sits at index 10, egress (08:30) at index 50.
    duration = 2.0
    pad_min = 30.0
    n = _n_samp_for(duration + 2 * pad_min / 60.0)
    assert n == 61
    ingress_idx = 10

    wins = [{
        "epoch": 0, "mid": "2026-03-15T07:30:00",
        "start": "2026-03-15T06:00:00", "end": "2026-03-15T09:00:00",
    }]

    # OK case: lsc covers from the grid start through well past ingress+30min
    # (index 25); cpt covers from before ingress through the end. No gap
    # (split_overlap), and lsc's own coverage brackets the ingress contact
    # cleanly on both sides.
    _patch_site_masks(monkeypatch, {"lsc": (0, ingress_idx + 15), "cpt": (ingress_idx + 8, n)})
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt"],
                               include_padding=True, pad_before_min=pad_min, pad_after_min=pad_min)
    assert res[0]["rating"] == "split_overlap"
    assert res[0]["ingress_bracket_ok"] is True

    # Failing case: lsc's coverage stops right at ingress itself, leaving no
    # post-ingress baseline from lsc alone -- the union still has no gap
    # (cpt already picks up just before ingress), but the bracket fails.
    _patch_site_masks(monkeypatch, {"lsc": (0, ingress_idx + 1), "cpt": (ingress_idx - 1, n)})
    res2 = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt"],
                                include_padding=True, pad_before_min=pad_min, pad_after_min=pad_min)
    assert res2[0]["rating"] == "split_overlap"
    assert res2[0]["ingress_bracket_ok"] is False


def test_site_coverage_span_detects_fragmentation():
    import numpy as np

    contiguous = np.array([False, True, True, True, False])
    fragmented = np.array([True, False, False, True, False])
    offsets = np.linspace(0.0, 1.0, len(contiguous))

    start, end, frag = T._site_coverage_span(contiguous, offsets, 0.0, 1.0)
    assert frag is False
    assert start == pytest.approx(0.25)
    assert end == pytest.approx(0.75)

    _, _, frag2 = T._site_coverage_span(fragmented, offsets, 0.0, 1.0)
    assert frag2 is True


# --------------------------------------------------------------------------- #
# visibility_series
# --------------------------------------------------------------------------- #


def test_visibility_series_structure():
    s = T.visibility_series(97.64, 29.67, "2026-03-15T10:00:00", 2.5, "ogg",
                            max_airmass=2.0, twilight="nautical", moon_sep_min=30)
    n = len(s["times"])
    assert n > 100
    for key in ("target_alt", "moon_alt", "sun_alt", "moon_sep"):
        assert len(s[key]) == n
    assert s["site"] == "ogg"
    assert s["alt_limit"] == pytest.approx(30.0, abs=1e-6)
    assert s["sun_alt_limit"] == -12.0
    assert s["ingress"] < s["mid"] if "mid" in s else s["ingress"] < s["egress"]
    assert s["ingress"] < s["egress"]
    assert 0.0 <= s["observable_fraction"] <= 1.0
    assert 0.0 <= s["moon_sep_mid"] <= 180.0


def test_visibility_series_observable_runs_match_mask():
    """observable_runs are the contiguous observable stretches of the plot grid,
    so the schedule page can offer the run around the transit as the window."""
    s = T.visibility_series(207.52600, -40.83590, "2026-07-27T01:30:00", 3.0, "lsc",
                            max_airmass=2.0, twilight="nautical", moon_sep_min=30)
    runs = s["observable_runs"]
    assert runs, "HIP67522 is observable from LSC that night"
    n_obs = 0
    for run in runs:
        assert run["start"] in s["times"] and run["end"] in s["times"]
        assert run["start"] <= run["end"]
        i0, i1 = s["times"].index(run["start"]), s["times"].index(run["end"])
        n_obs += i1 - i0 + 1
    # Together the runs cover exactly the observable samples.
    assert n_obs / len(s["times"]) == pytest.approx(s["observable_fraction"], abs=1e-3)
    # The run spanning mid-transit ends at the target's set (~03:14 at airmass 2).
    mid_run = next(r for r in runs if r["start"] <= "2026-07-27T01:30" <= r["end"])
    assert mid_run["end"].startswith("2026-07-27T03:1")


def test_true_runs_lists_every_run():
    assert T._true_runs([False, True, False, True, True, False]) == [(1, 1), (3, 4)]
    assert T._true_runs([True, True]) == [(0, 1)]
    assert T._true_runs([False, False]) == []


def test_visibility_series_rejects_unknown_site():
    with pytest.raises(T.TransitObsError):
        T.visibility_series(97.64, 29.67, "2026-03-15T10:00:00", 2.5, "jwst")


# --------------------------------------------------------------------------- #
# observable_interval / _longest_true_run
# --------------------------------------------------------------------------- #


def test_longest_true_run_picks_longer_leg():
    assert T._longest_true_run([False, True, False, True, True, True, False]) == (3, 5)
    assert T._longest_true_run([True, True, False, True]) == (0, 1)
    assert T._longest_true_run([True]) == (0, 0)


def test_observable_interval_clips_hip67522_set_edge():
    """HIP67522 at LSC: the window runs to 06:08 but the target sets below the
    airmass-2.0 limit by ~03:14, so the end is clipped and the start (already up)
    is preserved verbatim."""
    window = {"start": "2026-07-26T23:47:44.419201Z",
              "end": "2026-07-27T06:08:44.419201Z"}
    iv = T.observable_interval(207.52600, -40.83590, window, "lsc",
                               max_airmass=2.0, twilight="nautical", moon_sep_min=30.0)
    assert iv is not None
    # Start unchanged (target above the limit at window open) -> exact original.
    assert iv["start"] == window["start"]
    assert iv["hit_start_limit"] is False
    # End clipped by the target's set, ~03:14 UTC (well before 06:08).
    assert iv["hit_end_limit"] is True
    assert iv["end"].startswith("2026-07-27T03:1")
    assert 0.4 < iv["fraction"] < 0.7


def test_observable_interval_returns_none_when_unobservable():
    """No Sinistro site but LSC sees HIP67522 in this window; CPT gets nothing."""
    window = {"start": "2026-07-26T23:47:44Z", "end": "2026-07-27T06:08:44Z"}
    iv = T.observable_interval(207.52600, -40.83590, window, "cpt",
                               max_airmass=2.0, twilight="nautical", moon_sep_min=30.0)
    assert iv is None


def test_observable_interval_full_window_preserves_both_edges():
    """A window entirely within the observable run keeps both boundaries and
    flags neither edge as observability-bounded."""
    # A 40-min window centred near HIP67522 culmination at LSC (target high).
    window = {"start": "2026-07-27T00:00:00Z", "end": "2026-07-27T00:40:00Z"}
    iv = T.observable_interval(207.52600, -40.83590, window, "lsc",
                               max_airmass=2.0, twilight="nautical", moon_sep_min=30.0)
    assert iv is not None
    assert iv["start"] == window["start"]
    assert iv["end"] == window["end"]
    assert iv["hit_start_limit"] is False
    assert iv["hit_end_limit"] is False
    assert iv["fraction"] == pytest.approx(1.0, abs=1e-9)


def test_observable_interval_rejects_unknown_site():
    window = {"start": "2026-07-26T23:47:44Z", "end": "2026-07-27T06:08:44Z"}
    with pytest.raises(T.TransitObsError):
        T.observable_interval(207.526, -40.836, window, "jwst")


# --------------------------------------------------------------------------- #
# transit_plans: per-transit booking options (single site / either / relay)
# --------------------------------------------------------------------------- #

def _jd(iso):
    from astropy.time import Time
    return Time(iso.replace("Z", "")).jd


def test_relay_legs_hand_off_mid_overlap_with_a_margin():
    """Full early site + egress-only late site: the early leg is cut to just
    past the handoff instead of running the whole window, the late leg keeps
    its span, and the legs overlap by the relay margin."""
    d = 1 / 24.0
    early = (10.0 * d, 15.0 * d)   # sees everything
    late = (12.0 * d, 16.0 * d)    # starts mid-way
    a, b = T._relay_legs(early, late, overlap_min=30.0)
    # Handoff region is [12h, 15h]; midpoint 13.5h; +-15 min.
    assert a == pytest.approx((10.0 * d, 13.75 * d))
    assert b == pytest.approx((13.25 * d, 16.0 * d))


def test_relay_legs_keep_spans_when_a_gap_remains():
    d = 1 / 24.0
    a, b = T._relay_legs((10 * d, 12 * d), (12.5 * d, 15 * d), overlap_min=30.0)
    assert a == pytest.approx((10 * d, 12 * d)) and b == pytest.approx((12.5 * d, 15 * d))


def test_transit_plans_offer_each_site_either_and_a_relay():
    """TIC434398831 b, 2026-10-26 (rated 'Full @ tfn, cpt'): one plan per site
    that sees a contact, 'either' over both, and a relay whose early leg
    sees ingress and late leg sees egress."""
    window = {"mid": "2026-10-26T03:20:24Z", "start": "2026-10-26T01:12:32Z", "end": "2026-10-26T05:28:16Z"}
    plans = T.transit_plans(93.95926, 16.02385, [window], 2.76, max_airmass=2.0,
                            twilight="nautical", moon_sep_min=30.0)[0]
    by_id = {p["id"]: p for p in plans}
    assert by_id["site:tfn"]["coverage"] == "full"
    assert by_id["site:cpt"]["coverage"] == "partial"
    # Full sites are listed first.
    assert plans[0]["id"] == "site:tfn"
    either = by_id["either"]
    assert set(either["sites"]) == {"tfn", "cpt"}
    assert either["start"] <= by_id["site:tfn"]["start"] and either["end"] >= by_id["site:tfn"]["end"]
    relay = next(p for p in plans if p["type"] == "relay")
    early, late = relay["legs"]
    assert {early["site"], late["site"]} == {"tfn", "cpt"}
    ingress = _jd(window["mid"]) - 2.76 / 48
    egress = _jd(window["mid"]) + 2.76 / 48
    assert _jd(early["start"]) <= ingress and _jd(late["end"]) >= egress
    # Legs overlap at the handoff rather than both spanning the window.
    assert _jd(early["end"]) > _jd(late["start"])
    assert (_jd(early["end"]) - _jd(late["start"])) * 1440 <= 31


def test_transit_plans_skip_a_site_seeing_no_contact():
    """A window no site can observe yields no plans."""
    window = {"mid": "2026-10-29T19:47:00Z", "start": "2026-10-29T17:39:00Z", "end": "2026-10-29T21:54:00Z"}
    assert T.transit_plans(93.95926, 16.02385, [window], 2.76, max_airmass=2.0,
                           twilight="nautical", moon_sep_min=30.0) == [[]]


def test_transit_plans_relay_prefers_full_coverage_over_a_cheap_gapped_pair():
    """2026-11-13: ELP (ingress only) + COJ (egress only) leave ~111 min of the
    transit uncovered; the relay must instead hand OGG (full) over to COJ."""
    window = {"mid": "2026-11-13T13:33:54Z", "start": "2026-11-13T11:26:06Z", "end": "2026-11-13T15:41:42Z"}
    plans = T.transit_plans(93.95926, 16.02385, [window], 2.76, max_airmass=2.0,
                            twilight="nautical", moon_sep_min=30.0)[0]
    relay = next(p for p in plans if p["type"] == "relay")
    assert relay["gap_min"] == 0.0
    assert "ogg" in relay["sites"]


@pytest.mark.parametrize("values", [2461223.0, [2461223.0, 2461233.0]])
@pytest.mark.parametrize("failed_pass", [1, 2])
def test_bjd_conversion_retries_stale_iers(values, failed_pass, monkeypatch, caplog):
    import logging
    from astropy.time import Time
    from astropy.utils import iers

    expected = T.bjd_tdb_to_jd_utc(values, 228.45, -45.01)
    real_light_travel_time = Time.light_travel_time
    calls = []
    error = (
        "interpolating from IERS_Auto using predictive values that are more "
        "than 30.0 days old."
    )

    def fail_once(time, *args, **kwargs):
        calls.append((iers.conf.auto_download, iers.conf.auto_max_age))
        if len(calls) == failed_pass:
            raise ValueError(error)
        return real_light_travel_time(time, *args, **kwargs)

    monkeypatch.setattr(Time, "light_travel_time", fail_once)
    # The session fixture disables this path. Override it explicitly to check
    # the retry settings and restoration of the normal production policy.
    with iers.conf.set_temp("auto_download", True), iers.conf.set_temp("auto_max_age", 30):
        with caplog.at_level(logging.WARNING, logger="muscat_db.transit_obs"):
            result = T.bjd_tdb_to_jd_utc(values, 228.45, -45.01)
        assert iers.conf.auto_download is True
        assert iers.conf.auto_max_age == 30
    assert result == pytest.approx(expected, abs=1e-9)
    assert calls[failed_pass:] == [(False, None), (False, None)]
    assert error in caplog.text


@pytest.mark.parametrize("error", ["invalid coordinates", "IERS unrelated validation error"])
def test_bjd_conversion_does_not_retry_unrelated_value_errors(error, monkeypatch):
    from astropy.time import Time

    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise ValueError(error)

    monkeypatch.setattr(Time, "light_travel_time", fail)
    with pytest.raises(ValueError, match=error):
        T.bjd_tdb_to_jd_utc(2461223.0, 228.45, -45.01)
    assert len(calls) == 1
