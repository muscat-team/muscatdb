"""Previous Fit discovery must do visibility work only for actual outputs."""
import os
from types import SimpleNamespace

import pytest

from muscat_db import transit_fit as fit
from muscat_db import web


@pytest.mark.parametrize("restricted", [False, True])
def test_previous_runs_skip_empty_nights_and_preserve_order(monkeypatch, tmp_path, restricted):
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer"))
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose"))
    monkeypatch.setattr(web, "_render", lambda template, **context: context)
    monkeypatch.setattr(web, "_viewer_denied", lambda: frozenset({"private"}) if restricted else frozenset())
    monkeypatch.setattr(web, "_get_norm_name_overrides", lambda db: {})
    monkeypatch.setattr(web, "_viewer_may_modify_run", lambda *args: False)
    monkeypatch.setattr(fit, "get_target_parameters", lambda target: {})
    dates = [f"25{i:04d}" for i in range(1000)]
    monkeypatch.setattr(web, "_get_dates", lambda *args, **kwargs: [{"obsdate": d} for d in dates])
    checks = []

    def hidden(inst, date, target):
        checks.append((inst, date))
        return restricted and date == "250003"

    monkeypatch.setattr(web, "_night_visibility", lambda denied: SimpleNamespace(hidden=hidden))
    monkeypatch.setattr(web, "_product_visibility", lambda denied: SimpleNamespace(
        hidden=lambda level, inst, date, target, run: restricted and run == "private"))

    def put(inst, date, run=""):
        out = tmp_path / "timer" / inst / date / "HIP67522" / run / "out"
        out.mkdir(parents=True)
        (out / "fit.png").write_bytes(b"plot")
        stamp = 200 if run == "private" else 100
        os.utime(out, (stamp, stamp))

    put("muscat2", "250001")  # legacy
    put("muscat2", "250003", "private-night")
    put("muscat3", "250002", "public")
    put("muscat3", "250002", "private")
    put("muscat3", "260101", "ineligible")  # no obslog or prose date: stays excluded
    # Empty target directories and other targets do not need permission queries.
    (tmp_path / "timer/muscat2/250004/HIP67522").mkdir(parents=True)
    (tmp_path / "timer/muscat2/250005/another/out").mkdir(parents=True)

    result = web.transit_fit_page(target="HIP67522")
    rows = result["runs_json"]
    expected = [("muscat3", "250002", "public"), ("muscat2", "250001", "")]
    if not restricted:
        expected = [("muscat2", "250003", "private-night"),
                    ("muscat3", "250002", "private"), *expected]
    # Same-date runs retain newest-first ordering, independent of filesystem timing.
    assert [(r["inst"], r["date"], r["run_id"]) for r in rows] == expected
    assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True)
    assert rows[-1]["is_legacy"]
    assert set(checks) == {("muscat2", "250001"), ("muscat2", "250003"), ("muscat3", "250002")}

    # Discover newly completed fits immediately; no stale permission/result cache.
    put("muscat4", "250006", "new")
    assert web.transit_fit_page(target="HIP67522")["runs_json"][0]["run_id"] == "new"


def test_candidate_dates_validate_paths_and_refresh(monkeypatch, tmp_path):
    root = tmp_path / "timer"
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(root))
    (root / "muscat2/250001/HIP67522").mkdir(parents=True)
    (root / "muscat2/not-a-date/HIP67522").mkdir(parents=True)
    (root / "muscat2/２５０００２/HIP67522").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (root / "muscat2/250003").mkdir()
    (root / "muscat2/250003/HIP67522").symlink_to(external, target_is_directory=True)
    assert fit.target_fit_dates("muscat2", "HIP 67522") == ["250001"]
    for bad in ("../external", "a/b", "a\\b", "", ".."):
        assert fit.target_fit_dates("muscat2", bad) == []
        assert fit.target_fit_dates(bad, "HIP67522") == []
    assert fit.target_fit_dates("muscat3", "HIP67522") == []
    (root / "muscat2/250004/HIP67522").mkdir(parents=True)
    assert fit.target_fit_dates("muscat2", "HIP67522") == ["250004", "250001"]
