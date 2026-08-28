"""Regression test: a transit-fit/photometry job's ``target`` field strips
spaces from the raw object name for its filesystem-safe directory segment
(see ``jobs.target_dir_name``), so it never carries the spaces a norm_name
override was set on (e.g. via the targets page, which stores the raw
``targets.object`` spelling). ``get_norm_name_overrides`` must alias each
override under its whitespace-stripped form too, or ``_normalize_target_name``
silently misses the override for any such job -- which is what made the
ephemeris/TTV pages show no fit results for targets like
"TIC 110795273.01 (TOI 7504.01)".
"""

from __future__ import annotations

from muscat_db.catalog import _normalize_target_name
from muscat_db.database import get_norm_name_overrides, set_norm_name_override


def test_override_resolves_for_space_stripped_job_target(mock_db):
    raw_object = "TIC 110795273.01 (TOI 7504.01)"
    set_norm_name_override(mock_db, raw_object, "TOI7504")

    overrides = get_norm_name_overrides(mock_db)

    job_target = raw_object.replace(" ", "")  # jobs.target_dir_name behavior
    assert _normalize_target_name(job_target, overrides) == "TOI7504"
    assert _normalize_target_name(raw_object, overrides) == "TOI7504"
