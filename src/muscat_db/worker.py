"""Standalone job-queue worker (architecture issue #51, step 1: single-host).

The web process already runs every pipeline's ``sync_jobs()`` on a timer from
its own asyncio background task (see ``web._job_reconciliation_loop``): each
pass claims pending jobs via ``job_store.py``'s ``claim_slot``/``pending``
seam, launches them, and reconciles jobs it has already launched through to
``done``/``error``/``cancelled``. That reconciliation has never run anywhere
except inside the web process.

This module runs the exact same ``sync_jobs()`` functions on a timer, but as
an independent OS process with no FastAPI dependency. It proves the
claim/lease/finalize machinery in ``job_store.py`` is correct when driven from
a process other than the one serving HTTP -- the prerequisite for eventually
running it on a separate host (``notes/MUSCATDB-LITE.md`` §12) -- without yet
needing PostgreSQL, a second host, or any new infrastructure. ``claim_slot``'s
atomic INSERT already makes it safe to run this alongside the web process's
own reconciliation loop for the same pipeline: at most one of them ever wins
the claim for a given pending job.

Known limitation (left for step 3 -- lease/heartbeat -- rather than improvised
here): each pipeline's in-memory job registry (e.g. ``photometry._JOBS``) is
process-local. A job claimed and launched by *this* process is invisible to
the web process's registry, so cancelling it from the web UI does not yet
work -- the same gap the web process would have for a job launched by another
web worker under ``--workers N>1``. Jobs still queued (not yet claimed) cancel
fine either way, since that path only touches the durable ``jobs`` table.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _pipeline_registry() -> dict[str, Callable[[], None]]:
    # Imported lazily: photometry/transit_fit/ttv_fit each pull in prose2/
    # timer/harmonic command-building helpers, which unrelated CLI commands
    # (and `muscat-db worker --help`) should not pay to import.
    from muscat_db import photometry, transit_fit, ttv_fit

    return {
        "photometry": photometry.sync_jobs,
        "transit_fit": transit_fit.sync_jobs,
        "ttv_fit": ttv_fit.sync_jobs,
    }


def resolve_pipelines(pipeline: str) -> list[tuple[str, Callable[[], None]]]:
    """Resolve ``--pipeline`` -- a name, a comma-separated list of names, or
    ``"all"`` -- to an ordered list of ``(name, sync_jobs)`` pairs.

    Raises ``ValueError`` (never a bare KeyError) on an empty or unknown
    selection, so callers can report it as a normal usage error.
    """
    registry = _pipeline_registry()
    names = (
        list(registry)
        if pipeline == "all"
        else [p.strip() for p in pipeline.split(",") if p.strip()]
    )
    if not names:
        raise ValueError('--pipeline must name at least one pipeline, or "all"')
    unknown = [n for n in names if n not in registry]
    if unknown:
        raise ValueError(
            f"unknown pipeline(s) {unknown}; choose from {sorted(registry)} or 'all'"
        )
    return [(n, registry[n]) for n in names]


def run_pass(fns: list[tuple[str, Callable[[], None]]]) -> None:
    """Run one claim/reconcile pass over *fns*.

    Each pipeline's ``sync_jobs()`` is isolated: a failure in one is logged
    and skipped rather than raised, so one broken pipeline can never stop the
    others in the same pass or kill the worker loop.
    """
    for name, fn in fns:
        try:
            fn()
        except Exception:
            logger.exception("worker: reconciliation pass failed for %s", name)


def _loop(
    fns: list[tuple[str, Callable[[], None]]],
    *,
    interval: float,
    once: bool,
    stop_requested: Callable[[], bool],
) -> None:
    while True:
        run_pass(fns)
        if once or stop_requested():
            return
        time.sleep(interval)


def run(pipeline: str, *, interval: float = 2.0, once: bool = False) -> None:
    """Claim and launch *pipeline*'s pending jobs and reconcile jobs already
    launched, on a timer, until SIGTERM/SIGINT (or once, if *once*).

    *pipeline* is validated via :func:`resolve_pipelines` before anything
    else runs, so a typo fails fast instead of silently doing nothing.
    """
    fns = resolve_pipelines(pipeline)
    stop = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop
        logger.info("worker: received signal %d, stopping after this pass", signum)
        stop = True

    prev_handlers = None
    if not once:
        prev_handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
    try:
        _loop(fns, interval=interval, once=once, stop_requested=lambda: stop)
    finally:
        if prev_handlers is not None:
            signal.signal(signal.SIGTERM, prev_handlers[0])
            signal.signal(signal.SIGINT, prev_handlers[1])
