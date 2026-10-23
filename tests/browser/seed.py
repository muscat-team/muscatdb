"""Fixture data for the browser tests: one small, deterministic site.

Everything lives under one temp root so the app sees it through its normal
env vars (see ``conftest.live_server``). Targets, nights and jobs are chosen
so every page has something to render and filter:

* obslog frames for muscat2 (no WCS), muscat3 and sinistro over three nights;
* two targets, ``TOI-1000`` (in the fixture TOI and NExSci catalogs) and
  ``HAT-P-1`` (NExSci only);
* finished photometry / transit-fit / TTV job rows in the ``jobs`` table.
"""

from __future__ import annotations

import base64
import csv
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

ADMIN = "admin"
REPO_DATA = Path(__file__).resolve().parents[2] / "data"
# The only files under data/ that are tracked in git (so present on CI too).
TRACKED_DATA = ("FOV_MuSCAT.vot.xml", "FOV_MuSCAT2.vot.xml", "FOV_MuSCAT3.vot.xml")

# (instrument, obsdate, ccd, filename, object, jd_start, filter, ra, dec, proposal)
FRAMES = [
    ("muscat3", "261001", 0, "ogg2m001-ep05-20261001-0001.fits", "TOI-1000", 2461314.80, "gp", "07:51:43.0", "+12:32:11.0", "LCO2026B-001"),
    ("muscat3", "261001", 1, "ogg2m001-ep06-20261001-0001.fits", "TOI-1000", 2461314.80, "rp", "07:51:43.0", "+12:32:11.0", "LCO2026B-001"),
    ("muscat3", "261001", 0, "ogg2m001-ep05-20261001-0002.fits", "TOI-1000", 2461314.81, "gp", "07:51:43.0", "+12:32:11.0", "LCO2026B-001"),
    ("muscat2", "261002", 0, "MCT20_2610020001.fits", "HAT-P-1", 2461315.40, "g", "22:57:47.0", "+38:40:30.0", ""),
    ("muscat2", "261002", 1, "MCT21_2610020001.fits", "HAT-P-1", 2461315.40, "r", "22:57:47.0", "+38:40:30.0", ""),
    ("sinistro", "261003", 0, "lsc1m005-fa15-20261003-0010-e91.fits", "TOI-1000", 2461316.60, "ip", "07:51:43.0", "+12:32:11.0", "LCO2026B-002"),
]


@dataclass(frozen=True)
class Paths:
    root: Path
    db: Path
    data: Path
    raw: Path
    obslog: Path
    prose: Path
    timer: Path
    ttv: Path
    lco: Path
    tmp: Path


TAG = "demo"
TAG_DESCRIPTION = "Fixture project for the browser tests"
RUN_ID = "r1"
FIT_RUN_ID = "fit1"
# 1x1 transparent PNG, so served plots are real images.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Catalog rows by internal key; written under the CSV headers the loaders use
# (catalog._TOI_COLUMNS / _NEXSCI_COLUMNS), so the fixture follows the code.
# The ephemeris lookups (transit-fit priors, LCO windows) also read the extra
# columns below, which the catalog pages do not.
TOI_EXTRA = (("Epoch (BJD)", "epoch", "f"), ("Epoch (BJD) err", "epoch_err", "f"))
NEXSCI_EXTRA = (("pl_tranmid", "epoch", "f"), ("pl_letter", "letter", "s"))
TOI_ROWS = [
    dict(toi="1000.01", tic="50365310", name="", disp="PC", period=2.17, duration=2.1, depth=4300,
         radius=11.2, teq=1500, insol=850, tmag=9.6, steff=6100, srad=1.3, dist=180,
         ra=117.929, dec=12.536, epoch=2461314.86, epoch_err=0.0006, period_err=0.00002,
         duration_err=0.05),
    dict(toi="1001.01", tic="188350000", name="", disp="FP", period=1.93, duration=1.4, depth=900,
         radius=4.1, teq=900, insol=120, tmag=11.2, steff=5200, srad=0.9, dist=95,
         ra=120.5, dec=-30.2),
    dict(toi="1002.01", tic="260000001", name="HAT-P-1 b", disp="KP", period=4.465, duration=2.8,
         depth=14000, radius=14.0, teq=1320, insol=480, tmag=9.4, steff=5980, srad=1.17, dist=159,
         ra=344.446, dec=38.675, epoch=2454363.94656, epoch_err=0.00072),
    dict(toi="1003.01", tic="300000002", name="", disp="CP", period=12.4, duration=3.3, depth=2100,
         radius=6.0, teq=620, insol=25, tmag=10.1, steff=5600, srad=1.0, dist=60,
         ra=210.0, dec=5.0),
]
NEXSCI_ROWS = [
    dict(name="HAT-P-1 b", host="HAT-P-1", tic="TIC 260000001", method="Transit", facility="HATNet",
         letter="b", epoch=2454363.94656, trandur=2.8, year=2006, ra=344.446, dec=38.675, period=4.465, sma=0.0553, radius=14.0, mass=168.0,
         teq=1320, steff=5980, srad=1.17, dist=159, vmag=10.4, tmag=9.4, snum=2),
    dict(name="WASP-12 b", host="WASP-12", tic="TIC 86396382", method="Transit", facility="SuperWASP",
         letter="b", epoch=2454508.97605, trandur=3.0, year=2008, ra=97.636, dec=29.672, period=1.091, sma=0.0234, radius=21.3, mass=466.0,
         teq=2580, steff=6300, srad=1.66, dist=427, vmag=11.6, tmag=11.0, snum=3),
    dict(name="Kepler-51 b", host="Kepler-51", tic="TIC 417676622", method="Transit", facility="Kepler",
         year=2012, ra=297.0, dec=49.93, period=45.15, sma=0.25, radius=6.9, mass=3.7,
         teq=540, steff=5670, srad=0.88, dist=780, vmag=15.0, tmag=14.4, snum=1),
]

LIGHTCURVE = "BJD_TDB,Flux,Flux_err\n" + "".join(
    f"{2461314.80 + i * 0.002:.5f},{1.0 - (0.004 if 20 <= i < 40 else 0.0):.5f},0.001\n" for i in range(60)
)


def build_tree(root: Path) -> Paths:
    paths = Paths(
        root=root,
        db=root / "muscat.db",
        data=root / "data",
        raw=root / "raw",
        obslog=root / "obslog",
        prose=root / "prose",
        timer=root / "timer",
        ttv=root / "ttv",
        lco=root / "lco",
        tmp=root / "tmp",
    )
    for d in (paths.data, paths.raw, paths.obslog, paths.prose, paths.timer, paths.ttv, paths.lco, paths.tmp):
        d.mkdir(parents=True, exist_ok=True)
    for name in TRACKED_DATA:
        shutil.copy(REPO_DATA / name, paths.data / name)
    _build_db(paths.db)
    _make_raw_dirs(paths.raw)
    _write_catalogs(paths.data)
    _write_photometry_run(paths.prose)
    _write_fit_run(paths.timer)
    return paths


def _make_raw_dirs(raw: Path) -> None:
    """Empty raw-data night directories, so the pages offer to reduce them."""
    from muscat_db.instruments import INSTRUMENTS

    for inst, date in {(f[0], f[1]) for f in FRAMES}:
        (raw / INSTRUMENTS[inst].data_subdir / date).mkdir(parents=True, exist_ok=True)


def _write_csv(path: Path, columns, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([header for header, _, _ in columns])
        for row in rows:
            writer.writerow(["" if row.get(key) is None else row.get(key, "") for _, key, _ in columns])


def _write_catalogs(data: Path) -> None:
    from muscat_db import catalog

    _write_csv(data / "TOIs.csv", (*catalog._TOI_COLUMNS, *TOI_EXTRA), TOI_ROWS)
    _write_csv(data / "nexsci_pscomppars.csv", (*catalog._NEXSCI_COLUMNS, *NEXSCI_EXTRA), NEXSCI_ROWS)


def _write_photometry_run(prose: Path) -> None:
    """A finished muscat3 reduction of TOI-1000 on 261001 (run ``r1``)."""
    run = prose / "muscat3" / "261001" / "_runs" / "TOI-1000" / RUN_ID
    run.mkdir(parents=True)
    for band in ("gp", "rp"):
        (run / f"TOI-1000_muscat3_{band}_261001.csv").write_text(LIGHTCURVE)
        for kind in ("ref", "apertures", "cutouts", "alignment"):
            (run / f"TOI-1000_muscat3_{band}_261001_{kind}.png").write_bytes(PNG)
    for kind in ("lightcurves", "covariates", "stacks"):
        (run / f"TOI-1000_muscat3_261001_{kind}.png").write_bytes(PNG)
    (run / "_webrun_meta.json").write_text(json.dumps({"run_id": RUN_ID, "run_name": RUN_ID}))
    from muscat_db.photometry import _run_log_path

    _run_log_path(run, "muscat3", "261001", "TOI-1000", RUN_ID).write_text("photometry finished\n")


def _write_fit_run(timer: Path) -> None:
    """A finished transit fit of that reduction (run ``fit1``)."""
    import yaml

    run = timer / "muscat3" / "261001" / "TOI-1000" / FIT_RUN_ID
    (run / "out").mkdir(parents=True)
    (run / "out" / "fit.png").write_bytes(PNG)
    (run / "out" / "summary.csv").write_text(
        "parameter,mean,std\nt0[0],2461314.86,0.0004\nk[0],0.065,0.002\n"
    )
    (run / "meta.yaml").write_text(yaml.safe_dump(
        {"site": "ogg", "mode": "full", "run_name": FIT_RUN_ID, "run_id": FIT_RUN_ID}
    ))


def _build_db(db: Path) -> None:
    from muscat_db.coord import CoordRepr
    from muscat_db.database import SCHEMA, _insert_summary_rows, _populate_targets, _summary_rows

    conn = sqlite3.connect(db)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    conn.executemany(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa,
            proposal_id)
           VALUES (?, ?, ?, ?, ?, ?, '03:00:00', 30, 'fast', ?, ?, ?, 1.2, 0, NULL, ?)""",
        FRAMES,
    )
    _insert_summary_rows(conn, _summary_rows(conn))
    _populate_targets(conn)
    conn.execute(
        "INSERT INTO users (username, display_name, is_admin) VALUES (?, ?, 1)", (ADMIN, "Admin")
    )
    _seed_tags(conn)
    _seed_jobs(conn)
    conn.commit()
    conn.close()


def norm_name(obj: str) -> str:
    from muscat_db.catalog import _normalize_target_name

    return _normalize_target_name(obj)


def _seed_tags(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO tag_descriptions (tag, description) VALUES (?, ?)", (TAG, TAG_DESCRIPTION))
    conn.execute("INSERT INTO target_tags (norm_name, tag) VALUES (?, ?)", (norm_name("TOI-1000"), TAG))


def _seed_jobs(conn: sqlite3.Connection) -> None:
    now = time.time()
    rows = [
        ("photometry", "muscat3", "261001", "TOI-1000", "done", RUN_ID, now - 3600),
        ("transit_fit", "muscat3", "261001", "TOI-1000", "done", FIT_RUN_ID, now - 1800),
        ("photometry", "muscat2", "261002", "HAT-P-1", "error", "r2", now - 86400 * 3),
    ]
    for type_, inst, date, target, state, run_id, started in rows:
        conn.execute(
            """INSERT INTO jobs (key, type, instrument, obsdate, target, state, returncode,
                                 elapsed, started_at, error_desc, run_id, run_name, user_name, owner)
               VALUES (?, ?, ?, ?, ?, ?, ?, 300, ?, ?, ?, ?, ?, ?)""",
            (f"{type_}:{inst}/{date}/{target}/{run_id}", type_, inst, date, target, state,
             0 if state == "done" else 1, started, "" if state == "done" else "pipeline failed",
             run_id, run_id, ADMIN, ADMIN),
        )


def patch_app(mp: pytest.MonkeyPatch, paths: Paths) -> None:
    """Stub the app's live lookups that have no offline fallback."""
    mp.setattr("muscat_db.exposure.resolve_target_coords", lambda name: (117.929, 12.536))
    # Offline, every VizieR attempt fails at once; keep the retry loop but not
    # its back-off sleeps (seconds per lookup).
    mp.setattr("muscat_db.exposure._VIZIER_BACKOFF_SEC", 0)
