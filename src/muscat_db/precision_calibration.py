"""Compare MuSCAT3 differential photometry with the noise model at observed settings."""

import csv
from collections import defaultdict
from datetime import date
import json
import math
from pathlib import Path
from statistics import median, stdev
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from muscat_db import exposure, exposure_model, precision
from muscat_db.database import PRECISION_CALIBRATION_SCHEMA
from muscat_db.transit_obs import LCO_SITES

BIN_S = 600


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    instrument: Literal["muscat3"]
    target: str = Field(min_length=1)
    night: date
    band: Literal["gp", "rp", "ip", "zs"]
    mode: Literal["MUSCAT_FAST", "MUSCAT_SLOW"]
    csv_path: Path
    # Accepted for manifests made by the earlier binned estimator.
    oot_bjd: list[tuple[float, float]] | None = Field(default=None, exclude=True)
    mag: float = Field(ge=-10, le=40)
    comp_mags: list[float] = Field(min_length=1)
    exptime_s: float = Field(gt=0, lt=BIN_S)
    defocus_mm: float = Field(ge=0)
    read_noise_e: float = Field(ge=0)
    sky_rate: float = Field(ge=0)
    dark_rate: float = Field(ge=0)


def measure(product, path, model):
    if any(not -10 <= m <= 40 for m in product.comp_mags):
        raise ValueError("Invalid comparison magnitude")
    frames = []
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"BJD_TDB", "Flux", "Airmass"}.issubset(reader.fieldnames or []):
            raise ValueError("Expected BJD_TDB, Flux and Airmass columns")
        for row in reader:
            t, flux, airmass = (float(row[k]) for k in ("BJD_TDB", "Flux", "Airmass"))
            if not all(map(math.isfinite, (t, flux, airmass))) or flux <= 0 or airmass < 1:
                continue
            frames.append((t, flux, airmass))

    frames.sort()
    cycle = product.exptime_s + precision.readout_overhead(product.instrument, product.mode)
    differences = []
    pair_counts = [0] * len(frames)
    for i, (left, right) in enumerate(zip(frames, frames[1:])):
        gap = (right[0] - left[0]) * 86400
        if gap == 0:
            raise ValueError("Duplicate frame timestamps")
        if round(gap, 4) <= 1.5 * cycle:
            differences.append(right[1] - left[1])
            pair_counts[i] += 1
            pair_counts[i + 1] += 1
    if len(differences) < 2:
        raise ValueError("Need at least two adjacent frame pairs within 1.5 cadences")

    fluxes = []
    variance_sum = 0.0
    for (_, flux, airmass), count in zip(frames, pair_counts):
        if not count:
            continue
        fluxes.append(flux)
        budget = precision.noise_budget(
            model, product.mag, product.exptime_s, airmass=airmass,
            extinction=exposure.EXTINCTION[product.band],
            diameter_m=exposure.INSTRUMENT_PARAMS[product.instrument]["aperture_m"],
            altitude_m=LCO_SITES["ogg"][2], read_noise_e=product.read_noise_e,
            sky_rate=product.sky_rate, dark_rate=product.dark_rate,
            defocus_mm=product.defocus_mm, comp_mags=product.comp_mags,
        )
        # Each independent pair contributes both endpoint variances.
        variance_sum += count * budget.total**2

    frames_per_bin = BIN_S / cycle
    achieved = precision.ppm(stdev(differences) / median(fluxes) / math.sqrt(2 * frames_per_bin))
    predicted = precision.ppm(math.sqrt(variance_sum / (2 * len(differences) * frames_per_bin)))
    if not all(math.isfinite(rms) and rms > 0 for rms in (achieved, predicted)):
        raise ValueError("Invalid achieved or predicted RMS")
    return dict(
        product=product.model_dump(mode="json"),
        pairs=len(differences),
        predicted_ppm=predicted,
        achieved_ppm=achieved,
    )


def calibrate(conn, manifest):
    products = TypeAdapter(list[Product]).validate_json(manifest.read_bytes())
    if not products:
        raise ValueError("Empty manifest")
    models = exposure_model.models_for(conn, "muscat3")
    groups = defaultdict(list)
    keys, paths = set(), set()
    for p in products:
        key = p.target.strip().casefold(), p.night, p.band
        path = (manifest.parent / p.csv_path).resolve()
        night = p.night.strftime("%y%m%d")
        expected = f"{p.target}_{p.instrument}_{p.band}_{night}.csv"
        run_dir = path.parent
        run_id = ""
        if run_dir.parent.parent.name == "_runs":
            if run_dir.parent.name != p.target:
                raise ValueError(f"Run target disagrees with manifest: {path}")
            run_id = run_dir.name
            run_dir = run_dir.parents[2]
        if path.name != expected or run_dir.name != night or run_dir.parent.name != p.instrument:
            raise ValueError(f"Expected an archive product matching {expected}")
        if key in keys or path in paths:
            raise ValueError(f"Repeated product or target/night/band: {path.name}")
        keys.add(key)
        paths.add(path)
        model = models.get((p.band, exposure_model.mode_key(p.instrument, p.mode)))
        if model is None:
            raise ValueError(f"No model for {p.band} {p.mode}")
        if p.defocus_mm > model.max_defocus_mm:
            raise ValueError(f"{path.name}: defocus exceeds the calibrated range")
        try:
            sample = measure(p, path, model)
            sample["model"] = model.as_dict()
            sample["run_id"] = run_id
            groups[p.instrument, p.band, p.mode].append(sample)
        except ValueError as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
    results = []
    for key, samples in groups.items():
        nights = sorted({s["product"]["night"] for s in samples})
        if len(nights) < 3:
            raise ValueError(f"{key}: need at least three distinct nights")
        cutoff = nights[-math.ceil(len(nights) / 5)]
        ratios = []
        for sample in samples:
            sample["held_out"] = sample["product"]["night"] >= cutoff
            if not sample["held_out"]:
                ratios.append(sample["achieved_ppm"] / sample["predicted_ppm"])
        results.append((*key, median(ratios), json.dumps(samples, allow_nan=False)))
    conn.execute(PRECISION_CALIBRATION_SCHEMA)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO precision_calibration VALUES (?, ?, ?, ?, ?)", results
        )


def visible_comparisons(conn, visibility):
    groups = []
    for inst, band, mode, factor, payload in conn.execute(
        "SELECT instrument, band, mode, factor, samples_json FROM precision_calibration "
        "ORDER BY instrument, band, mode"
    ):
        samples = json.loads(payload)
        rows = []
        fit_visible = True
        for s in samples:
            p = s["product"]
            night = date.fromisoformat(p["night"]).strftime("%y%m%d")
            if visibility.hidden("photometry", inst, night, p["target"], s["run_id"]):
                if not s["held_out"]:
                    fit_visible = False
                continue
            rows.append(s)
        if rows:
            groups.append(
                dict(
                    instrument=inst,
                    band=band,
                    mode=mode,
                    factor=factor if fit_visible else None,
                    rows=sorted(rows, key=lambda s: s["product"]["night"]),
                )
            )
    return groups
