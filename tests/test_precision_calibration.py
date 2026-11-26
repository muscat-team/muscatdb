import json
import random
import sqlite3
from statistics import median, stdev
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from muscat_db import exposure, exposure_model, precision
from muscat_db.cli import app
from muscat_db.database import _APP_OWNED_TABLES
from muscat_db.precision_calibration import visible_comparisons
from muscat_db.transit_obs import LCO_SITES


def test_archive_calibration_and_holdout(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    model = exposure_model.models_for(conn, "muscat3")["rp", ""]
    # 54 s integrations plus 6 s readout give ten frames per wall-time bin.
    frame = precision.noise_budget(
        model,
        12,
        54,
        airmass=1.2,
        extinction=exposure.EXTINCTION["rp"],
        diameter_m=exposure.INSTRUMENT_PARAMS["muscat3"]["aperture_m"],
        altitude_m=LCO_SITES["ogg"][2],
        read_noise_e=5,
        sky_rate=2,
        comp_mags=[11],
    )
    expected = frame.total / 10**0.5
    products = []
    reference = []
    for n, injected in enumerate([1.0, 1.0, 7.0], 1):
        night = f"25010{n}"
        root = tmp_path / "muscat3" / night / "_runs" / "Star" / "default"
        root.mkdir(parents=True)
        path = root / f"Star_muscat3_rp_{night}.csv"
        start = 2460676.5 + n
        rows = ["BJD_TDB,Flux,Airmass"]
        rng = random.Random(n)
        fluxes, diffs = [], []
        for i in range(300):
            # A 0.5 ppt/hour drift and a level change across a weather gap.
            minute = i if i < 150 else i + 30
            flux = 1 + 0.0005 * minute / 60 + rng.gauss(0, injected * frame.total)
            if i >= 150:
                flux += 0.02
            if i not in (0, 150):
                diffs.append(flux - fluxes[-1])
            fluxes.append(flux)
            rows.append(f"{start + minute / 1440},{flux},1.2")
        # This isolated frame has no eligible neighbour.
        rows.append(f"{start + 0.5},1,4")
        reference.append(stdev(diffs) / median(fluxes) / 20**0.5 * 1e6)
        path.write_text("\n".join(rows))
        products.append(
            dict(
                csv_path=str(path.relative_to(tmp_path)),
                target="Star",
                instrument="muscat3",
                band="rp",
                mode="MUSCAT_FAST",
                night=f"2025-01-0{n}",
                mag=12,
                comp_mags=[11],
                exptime_s=54,
                defocus_mm=0,
                read_noise_e=5,
                sky_rate=2,
                dark_rate=0,
            )
        )
    # Older manifests may still supply an OOT interval.
    products[0]["oot_bjd"] = [[2460677.5, 2460677.51]]
    manifest = tmp_path / "products.json"
    manifest.write_text(json.dumps(products))

    def run():
        return CliRunner().invoke(app, ["calibrate-precision", str(manifest), "--db", str(db)])

    result = run()
    assert result.exit_code == 0, (result.output, result.exception)
    visible = Mock()
    visible.hidden.return_value = False
    (group,) = visible_comparisons(conn, visible)
    expected_factor = median(reference[:2]) / (expected * 1e6)
    assert group["factor"] == pytest.approx(expected_factor)
    assert group["factor"] == pytest.approx(1, abs=0.1)
    assert [s["held_out"] for s in group["rows"]] == [False, False, True]
    held = group["rows"][-1]
    assert held["pairs"] == 298
    assert held["predicted_ppm"] == pytest.approx(expected * 1e6)
    assert held["achieved_ppm"] == pytest.approx(reference[-1])
    assert "precision_calibration" in _APP_OWNED_TABLES
    visible.hidden.assert_called_with("photometry", "muscat3", "250103", "Star", "default")
    visible.hidden.side_effect = lambda level, inst, night, target, run: night == "250101"
    (redacted,) = visible_comparisons(conn, visible)
    assert redacted["factor"] is None
    assert len(redacted["rows"]) == 2

    manifest.write_text(json.dumps(products + products[:1]))
    assert run().exit_code != 0
    path.write_text("\n".join(rows[:3]))
    manifest.write_text(json.dumps(products))
    assert "two adjacent" in run().output
    assert conn.execute("SELECT factor FROM precision_calibration").fetchone()[0] == pytest.approx(
        expected_factor
    )
    conn.close()
