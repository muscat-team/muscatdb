"""Product visibility computed from the per-run record (#144 PR 5b, PR 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from muscat_db import product_record as pr
from muscat_db.database import ScopedDenied
from muscat_db.product_visibility import ProductVisibility

INST, DATE, TARGET = "sinistro", "260101", "TOI-1234"
RESTRICTED = "KEY2026B-001"
OPEN = "OTHER-9"
DENIED = frozenset({RESTRICTED})


class _Nights:
    """Stands in for NightVisibility: hides the targets in *hidden_targets*."""

    def __init__(self, hidden_targets=()):
        self.hidden_targets = set(hidden_targets)

    def hidden(self, inst, date, target):
        return target in self.hidden_targets


def _frames(*proposals, resolved=True):
    return {
        "kind": "frames", "instrument": INST, "obsdate": DATE, "target": TARGET,
        "proposal_ids": sorted(proposals), "n_frames": 3, "blank_frames": 0,
        "resolved": resolved,
    }


def _viewer(tmp_path: Path, user="viewer", denied=DENIED, nights=None):
    return ProductVisibility(
        user, denied, nights or _Nights(),
        run_dir=lambda level, inst, date, target, run_id: tmp_path / level / inst / date / target / run_id,
    )


def _put(tmp_path, level, inputs, *, author="someone", target=TARGET, run_id="r1", date=DATE):
    rdir = tmp_path / level / INST / date / target / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    assert pr.write_record(rdir, pr.build_record(level, author=author, inputs=inputs))


def test_nothing_denied_shows_a_run_with_no_record(tmp_path):
    v = _viewer(tmp_path, denied=frozenset())
    assert not v.hidden("transit_fit", INST, DATE, TARGET, "r1")


def test_run_with_no_record_is_admin_only_when_something_is_restricted(tmp_path):
    assert _viewer(tmp_path).hidden("transit_fit", INST, DATE, TARGET, "r1")


def test_run_is_visible_when_every_input_is(tmp_path):
    _put(tmp_path, "photometry", [_frames(OPEN)])
    assert not _viewer(tmp_path).hidden("photometry", INST, DATE, TARGET, "r1")


def test_run_is_hidden_when_any_input_is_denied(tmp_path):
    _put(tmp_path, "photometry", [_frames(OPEN, RESTRICTED)])
    assert _viewer(tmp_path).hidden("photometry", INST, DATE, TARGET, "r1")


def test_denied_match_ignores_case(tmp_path):
    _put(tmp_path, "photometry", [_frames(RESTRICTED.lower())])
    assert _viewer(tmp_path).hidden("photometry", INST, DATE, TARGET, "r1")


def test_author_sees_their_own_run_even_with_denied_inputs(tmp_path):
    _put(tmp_path, "photometry", [_frames(RESTRICTED)], author="Alice")
    assert not _viewer(tmp_path, user="alice").hidden("photometry", INST, DATE, TARGET, "r1")


def test_author_sees_their_own_undetermined_run(tmp_path):
    _put(tmp_path, "photometry", None, author="alice")
    assert not _viewer(tmp_path, user="alice").hidden("photometry", INST, DATE, TARGET, "r1")


def test_undetermined_inputs_are_admin_only(tmp_path):
    _put(tmp_path, "photometry", None)
    assert _viewer(tmp_path).hidden("photometry", INST, DATE, TARGET, "r1")


def test_unresolved_frames_input_is_hidden(tmp_path):
    _put(tmp_path, "photometry", [_frames(resolved=False)])
    assert _viewer(tmp_path).hidden("photometry", INST, DATE, TARGET, "r1")


def test_blank_propid_is_denied_for_a_scoped_viewer_only(tmp_path):
    _put(tmp_path, "photometry", [_frames("")])
    scoped = ScopedDenied({RESTRICTED, ""}, frozenset({OPEN}))
    assert _viewer(tmp_path, denied=scoped).hidden("photometry", INST, DATE, TARGET, "r1")
    assert not _viewer(tmp_path).hidden("photometry", INST, DATE, TARGET, "r1")


def test_unknown_input_kind_fails_closed(tmp_path):
    _put(tmp_path, "photometry", [{"kind": "mystery"}])
    assert _viewer(tmp_path).hidden("photometry", INST, DATE, TARGET, "r1")


def test_record_of_another_level_is_not_trusted(tmp_path):
    rdir = tmp_path / "transit_fit" / INST / DATE / TARGET / "r1"
    rdir.mkdir(parents=True)
    pr.write_record(rdir, pr.build_record("photometry", author="x", inputs=[_frames(OPEN)]))
    assert _viewer(tmp_path).hidden("transit_fit", INST, DATE, TARGET, "r1")


def test_unsafe_name_is_hidden(tmp_path):
    def bad(*_a):
        raise ValueError("invalid target")

    v = ProductVisibility("viewer", DENIED, _Nights(), run_dir=bad)
    assert v.hidden("transit_fit", INST, DATE, "../x", "r1")


def test_legacy_photometry_uses_the_night_rule_not_the_record(tmp_path):
    # The shared night directory holds one record for every target; it is ignored.
    _put(tmp_path, "photometry", [_frames(RESTRICTED)], run_id="")
    assert not _viewer(tmp_path, nights=_Nights()).hidden("photometry", INST, DATE, TARGET, "")
    assert _viewer(tmp_path, nights=_Nights({TARGET})).hidden("photometry", INST, DATE, TARGET, "")


def test_transit_fit_follows_its_light_curve_runs(tmp_path):
    _put(tmp_path, "photometry", [_frames(OPEN)], run_id="open")
    _put(tmp_path, "photometry", [_frames(RESTRICTED)], run_id="shut")
    lc = lambda rid: {  # noqa: E731
        "kind": "product", "level": "photometry", "instrument": INST,
        "obsdate": DATE, "target": TARGET, "run_id": rid,
    }
    _put(tmp_path, "transit_fit", [lc("open")], run_id="ok")
    _put(tmp_path, "transit_fit", [lc("open"), lc("shut")], run_id="mixed")
    v = _viewer(tmp_path)
    assert not v.hidden("transit_fit", INST, DATE, TARGET, "ok")
    assert v.hidden("transit_fit", INST, DATE, TARGET, "mixed")


def test_ttv_fit_hides_when_one_fit_it_used_is_hidden(tmp_path):
    """The case #208 describes: a stored model fitted to a night the viewer cannot see."""
    _put(tmp_path, "photometry", [_frames(OPEN)], run_id="open", date="260101")
    _put(tmp_path, "photometry", [_frames(RESTRICTED)], run_id="shut", date="260102")
    _put(tmp_path, "transit_fit", [{
        "kind": "product", "level": "photometry", "instrument": INST,
        "obsdate": "260101", "target": TARGET, "run_id": "open"}], run_id="f1", date="260101")
    _put(tmp_path, "transit_fit", [{
        "kind": "product", "level": "photometry", "instrument": INST,
        "obsdate": "260102", "target": TARGET, "run_id": "shut"}], run_id="f2", date="260102")

    def fit(date, rid):
        return {"kind": "product", "level": "transit_fit", "instrument": INST,
                "obsdate": date, "target": TARGET, "run_id": rid}

    _put(tmp_path, "ttv_fit", [fit("260101", "f1")], run_id="visible")
    _put(tmp_path, "ttv_fit", [fit("260101", "f1"), fit("260102", "f2")], run_id="both")
    _put(tmp_path, "ttv_fit", [{"kind": "manual", "note": "typed"}], run_id="typed")
    v = _viewer(tmp_path)
    assert not v.hidden("ttv_fit", INST, DATE, TARGET, "visible")
    assert v.hidden("ttv_fit", INST, DATE, TARGET, "both")
    assert not v.hidden("ttv_fit", INST, DATE, TARGET, "typed")


def test_cyclic_product_inputs_fail_closed(tmp_path):
    self_ref = {"kind": "product", "level": "transit_fit", "instrument": INST,
                "obsdate": DATE, "target": TARGET, "run_id": "r1"}
    _put(tmp_path, "transit_fit", [self_ref])
    assert _viewer(tmp_path).hidden("transit_fit", INST, DATE, TARGET, "r1")


@pytest.mark.parametrize("level", ["photometry", "transit_fit", "ttv_fit"])
def test_every_level_is_admin_only_without_a_record(tmp_path, level):
    assert _viewer(tmp_path).hidden(level, INST, DATE, TARGET, "nope")


def test_job_hidden_uses_the_night_before_the_run_starts_and_the_record_after(tmp_path):
    v = _viewer(tmp_path, nights=_Nights({TARGET}))
    # No run directory yet: the night it will read decides.
    assert v.job_hidden("photometry", INST, DATE, TARGET, "r1")
    assert not _viewer(tmp_path).job_hidden("photometry", INST, DATE, TARGET, "r1")
    # A TTV job has no night, so without a directory it stays hidden.
    assert _viewer(tmp_path).job_hidden("ttv_fit", "", "", TARGET, "r1")
    _put(tmp_path, "photometry", [_frames(OPEN)])
    assert not v.job_hidden("photometry", INST, DATE, TARGET, "r1")


def test_a_three_level_chain_is_followed(tmp_path):
    def link(level, rid):
        return {"kind": "product", "level": level, "instrument": INST,
                "obsdate": DATE, "target": TARGET, "run_id": rid}

    _put(tmp_path, "photometry", [_frames(RESTRICTED)], run_id="p")
    _put(tmp_path, "transit_fit", [link("photometry", "p")], run_id="f")
    _put(tmp_path, "ttv_fit", [link("transit_fit", "f")], run_id="t")
    assert _viewer(tmp_path).hidden("ttv_fit", INST, DATE, TARGET, "t")
    _put(tmp_path, "photometry", [_frames(OPEN)], run_id="p")
    assert not _viewer(tmp_path).hidden("ttv_fit", INST, DATE, TARGET, "t")
