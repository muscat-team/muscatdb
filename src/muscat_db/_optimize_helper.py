"""Standalone helper for an optimize-only ``timer`` test run (no MCMC).

This runs INSIDE the ``timer`` conda env (which provides ``pymc`` and the
``timer`` package); it is *not* imported by the web app for its timer calls.
The web app launches it as the test-run process::

    <timer-env-python> _optimize_helper.py <work_dir>

``<work_dir>`` must contain ``fit.yaml``, ``sys.yaml`` and the light-curve CSVs
(exactly what ``transit_fit.start_fit`` prepares). It drives timer's own API
through the steps of ``timer-fit`` that precede sampling:

* ``plot_data``      -> ``out/data.png``
* ``build_model``    -> MAP optimization, ``out/fit.png`` (MAP model)
* ``clip_outliers``  -> re-optimizes if the outlier mask changed

then, instead of sampling, plots the per-dataset systematics from the MAP
solution (``out/sys-<name>.png``) and logs the MAP parameter values. This is a
quick visual check of the entered parameters, not an inference: there is no
``summary.csv``, corner or trace plot.

On success the last log line is :data:`DONE_MARKER`, which the web app treats
as the run's terminal result line.
"""
from __future__ import annotations

import logging
import os
import sys
import time

DONE_MARKER = "Optimize-only test run completed successfully"

# Per-point model arrays in the MAP solution; one value per data point (or per
# high-resolution model point), so they are not parameters worth listing.
_PER_POINT_SUFFIXES = (
    "_lm", "_light_curves", "_light_curves_hr", "_lc_pred", "_gp_pred", "_flare", "_bump",
)
# Anything larger than this is an array, not a parameter, whatever its name.
_MAX_PARAM_SIZE = 16


def _format_value(value: float) -> str:
    """Format a MAP value with at most 6 decimals (no uncertainty is available)."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def format_map_params(map_soln: dict) -> list[str]:
    """Return ``name = value`` lines for the parameters in a MAP solution.

    Transformed variables (``*__``) and per-point model arrays are skipped;
    vector parameters (one entry per planet or per regressor) are expanded as
    ``name[i]``.
    """
    import numpy as np

    lines = []
    for name in sorted(map_soln):
        if name.endswith("__") or name.endswith(_PER_POINT_SUFFIXES):
            continue
        try:
            values = np.atleast_1d(np.asarray(map_soln[name], dtype=float)).ravel()
        except (TypeError, ValueError):
            continue  # non-numeric entry (e.g. a GP object), not a parameter
        if values.size == 0 or values.size > _MAX_PARAM_SIZE:
            continue
        if values.size == 1:
            lines.append(f"{name} = {_format_value(values[0])}")
        else:
            lines.extend(f"{name}[{i}] = {_format_value(v)}" for i, v in enumerate(values))
    return lines


def run(work_dir: str, outdir: str = "out") -> None:
    """Optimize the model in ``work_dir`` and write the MAP plots, no sampling."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from timer.fit import setup_logging

    plt.rcParams["figure.dpi"] = 150
    os.makedirs(os.path.join(work_dir, outdir), exist_ok=True)
    setup_logging(os.path.join(work_dir, outdir), verbose=True)
    tick = time.time()

    logging.info("Optimize-only test run started (no MCMC sampling)")
    fit = _load(work_dir, outdir)
    # Without clobber, timer loads a trace.pkl left by an earlier full fit and
    # plots the posterior instead; a test run must show the MAP model only.
    fit.trace = None
    logging.info("Plotting data")
    fit.plot_data()
    logging.info("Building and optimizing model")
    fit.build_model(verbose=True)
    logging.info("Clipping outliers")
    # re-optimizes and re-plots fit.png itself when the outlier mask changed
    fit.clip_outliers()
    plt.close("all")

    for name in fit.data:
        logging.info("Plotting systematics for %s", name)
        try:
            fit.plot_systematics(name, fn=f"sys-{name}.png")
        except Exception as exc:  # optional extra; the MAP fit plot already exists
            logging.warning("Systematics plot for %s failed: %s", name, exc)
        plt.close("all")

    logging.info("MAP parameters:")
    for line in format_map_params(fit.map_soln):
        logging.info("  %s", line)

    logging.info("%s in %.0f seconds", DONE_MARKER, time.time() - tick)


def _load(work_dir: str, outdir: str):
    """Build a TransitFit exactly as the ``timer-fit`` CLI does."""
    import yaml
    from timer.fit import TransitFit

    with open(os.path.join(work_dir, "fit.yaml")) as f:
        fit_params = yaml.load(f, Loader=yaml.FullLoader)
    with open(os.path.join(work_dir, "sys.yaml")) as f:
        sys_params = yaml.load(f, Loader=yaml.FullLoader)
    return TransitFit(sys_params, fit_params, wd=work_dir, outdir=outdir)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: _optimize_helper.py <work_dir>")
        return 2
    try:
        run(argv[1])
    except Exception as exc:  # surface any failure to the calling process
        logging.error("Optimize-only test run failed: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
