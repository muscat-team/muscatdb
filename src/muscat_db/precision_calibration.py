"""Compare MuSCAT3 differential photometry with the noise model at observed settings."""

import csv
from collections import defaultdict
from datetime import date
import json
import math
from pathlib import Path
from statistics import mean, median
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
    oot_bjd: list[tuple[float, float]] = Field(min_length=1)
    mag: float = Field(ge=-10, le=40)
    comp_mags: list[float] = Field(min_length=1)
    exptime_s: float = Field(gt=0, lt=BIN_S)
    defocus_mm: float = Field(ge=0)
    read_noise_e: float = Field(ge=0)
    sky_rate: float = Field(ge=0)
    dark_rate: float = Field(ge=0)


def _oot_bins(product, path):
    """Group valid frames into wall-time bins contained in one OOT interval."""
    windows = sorted(product.oot_bjd)
    if any(lo >= hi for lo, hi in windows) or any(
        right[0] < left[1] for left, right in zip(windows, windows[1:])
    ):
        raise ValueError("OOT windows must be ordered pairs without overlap")
    if any(not -10 <= m <= 40 for m in product.comp_mags):
        raise ValueError("Invalid comparison magnitude")
    bins = defaultdict(list)
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"BJD_TDB", "Flux", "Airmass"}.issubset(reader.fieldnames or []):
            raise ValueError("Expected BJD_TDB, Flux and Airmass columns")
        for row in reader:
            t, flux, airmass = (float(row[k]) for k in ("BJD_TDB", "Flux", "Airmass"))
            if not all(map(math.isfinite, (t, flux, airmass))) or flux <= 0 or airmass < 1:
                continue
            for lo, hi in windows:
                if lo <= t < hi:
                    index = math.floor(round((t - lo) * 86400, 4) / BIN_S)
                    if (index + 1) * BIN_S <= round((hi - lo) * 86400, 4):
                        bins[lo, index].append((t, flux, airmass))
                    break

    return bins


def measure(product, path, model):
    bins = _oot_bins(product, path)
    cycle = product.exptime_s + precision.readout_overhead(product.instrument, product.mode)
    means, variances, fluxes = [], [], []
    for (lo, index), frames in sorted(bins.items()):
        times = sorted(frame[0] for frame in frames)
        if len(set(times)) != len(times):
            raise ValueError("Duplicate frame timestamps")
        start = lo + index * BIN_S / 86400
        gaps = [86400 * (b - a) for a, b in zip(times, times[1:])]
        gaps.extend([(times[0] - start) * 86400, BIN_S - (times[-1] - start) * 86400])
        if len(times) < 2 or max(gaps) > 1.5 * cycle:
            continue
        fluxes.extend(f for _, f, _ in frames)
        means.append(mean(f for _, f, _ in frames))
        # Independent per-frame variances add. Use the actual frame count and
        # airmasses in this wall-time bin, including real readout losses.
        frame_variances = []
        for _, _, airmass in frames:
            budget = precision.noise_budget(
                model,
                product.mag,
                product.exptime_s,
                airmass=airmass,
                extinction=exposure.EXTINCTION[product.band],
                diameter_m=exposure.INSTRUMENT_PARAMS[product.instrument]["aperture_m"],
                altitude_m=LCO_SITES["ogg"][2],
                read_noise_e=product.read_noise_e,
                sky_rate=product.sky_rate,
                dark_rate=product.dark_rate,
                defocus_mm=product.defocus_mm,
                comp_mags=product.comp_mags,
            )
            frame_variances.append(budget.total**2)
        variances.append(sum(frame_variances) / len(frames) ** 2)
    if len(means) < 2:
        raise ValueError("Need at least two complete out-of-transit bins")
    baseline = median(fluxes)
    achieved = precision.ppm(math.sqrt(mean((v / baseline - 1) ** 2 for v in means)))
    predicted = precision.ppm(math.sqrt(mean(variances)))
    if not all(math.isfinite(rms) and rms > 0 for rms in (achieved, predicted)):
        raise ValueError("Invalid achieved or predicted RMS")
    return dict(
        product=product.model_dump(mode="json"),
        bins=len(means),
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
