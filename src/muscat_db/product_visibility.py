"""Who may see a data product, computed from its record (issue #144, #208).

A photometry run, a transit fit and a TTV fit each store a ``_product.json``
(see :mod:`muscat_db.product_record`) naming the frames or upstream products it
was made from. One rule applies at every level:

* nothing is restricted for this viewer (admins, or no restricted proposal):
  visible, as before;
* the product's author always sees it;
* no readable record, or ``inputs: null`` (undetermined): admin-only;
* otherwise visible only when *every* input is. A frames input is visible when
  all of its PROPIDs are, and a blank PROPID counts as denied for a scoped
  viewer (:func:`muscat_db.access.is_denied`). A product input is visible when
  that upstream product is. A manual input has no frames behind it.

The exception is a legacy photometry run (no run id): it writes into the
night's directory, shared by every target of that night, so one record cannot
describe it. It keeps the night/target rule (:class:`NightVisibility`) until it
is re-run under a run id.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from muscat_db import product_record
from muscat_db.access import NightVisibility, is_denied

# Longest chain of product inputs followed (TTV -> transit fit -> photometry is
# two hops). A chain this long is a corrupt record, and fails closed.
_MAX_DEPTH = 4

RunDirResolver = Callable[[str, str, str, str, str], Path]


def default_run_dir(level: str, inst: str, date: str, target: str, run_id: str) -> Path:
    """The directory holding a run's output and record. May raise ValueError
    for a name that does not resolve to a safe path."""
    if level == "photometry":
        from muscat_db import photometry

        return photometry.run_output_dir(inst, date, target, run_id or None)
    if level == "transit_fit":
        from muscat_db import transit_fit

        return transit_fit.fit_output_dir(inst, date, target, run_id or None)
    if level == "ttv_fit":
        from muscat_db import ttv_fit

        return ttv_fit.ttv_output_dir(target, run_id)
    raise ValueError(f"unknown product level {level!r}")


class ProductVisibility:
    """Per-request product visibility for one viewer.

    Build one per request: it holds the viewer's denied set and the
    :class:`NightVisibility` used for legacy photometry runs.
    """

    def __init__(
        self,
        username: str | None,
        denied: frozenset[str],
        nights: NightVisibility,
        run_dir: RunDirResolver = default_run_dir,
    ) -> None:
        self.username = (username or "").strip().casefold()
        self.denied = denied
        self.nights = nights
        self._run_dir = run_dir
        self._memo: dict[tuple[str, str, str, str, str], bool] = {}

    def hidden(
        self, level: str, inst: str, date: str, target: str, run_id: str = "", *, _depth: int = 0
    ) -> bool:
        """True when the viewer may not see this run."""
        if not self.denied:
            return False
        if _depth:  # a result cut short by the depth limit must not be cached
            return self._compute_hidden(level, inst, date, target, run_id, _depth)
        key = (level, inst, date, target, run_id)
        if key not in self._memo:
            self._memo[key] = self._compute_hidden(level, inst, date, target, run_id, 0)
        return self._memo[key]

    def _compute_hidden(self, level: str, inst: str, date: str, target: str, run_id: str, depth: int) -> bool:
        if level == "photometry" and not run_id:
            return self.nights.hidden(inst, date, target)
        if depth > _MAX_DEPTH:
            return True
        try:
            rdir = self._run_dir(level, inst, date, target, run_id)
        except ValueError:
            return True
        record = product_record.read_record(rdir)
        if record is None or record.get("level") != level:
            return True
        return not self._record_visible(record, depth)

    def job_hidden(self, level: str, inst: str, date: str, target: str, run_id: str) -> bool:
        """Like :meth:`hidden`, for a row of the job table.

        A job that has not written its run directory yet (queued, or failed
        before output) has no record to read. A photometry or transit-fit job
        is then gated by the night it will read, even for its owner, since
        access may have changed since it was queued; a TTV job has no night,
        so it stays hidden.
        """
        if not self.denied:
            return False
        try:
            started = self._run_dir(level, inst, date, target, run_id).exists()
        except ValueError:
            return True
        if not started:
            return level == "ttv_fit" or self.nights.hidden(inst, date, target)
        return self.hidden(level, inst, date, target, run_id)

    def _record_visible(self, record: dict, depth: int) -> bool:
        author = str(record.get("author") or "").strip().casefold()
        if author and author == self.username:
            return True
        inputs = record.get("inputs")
        if inputs is None:
            return False
        return all(self._input_visible(entry, depth) for entry in inputs)

    def _input_visible(self, entry: object, depth: int) -> bool:
        if not isinstance(entry, dict):
            return False
        kind = entry.get("kind")
        if kind == "manual":
            return True
        if kind == "frames":
            proposals = entry.get("proposal_ids")
            if not entry.get("resolved") or not isinstance(proposals, list) or not proposals:
                return False
            return not any(is_denied(p, self.denied) for p in proposals)
        if kind == "product":
            return not self.hidden(
                str(entry.get("level") or ""),
                str(entry.get("instrument") or ""),
                str(entry.get("obsdate") or ""),
                str(entry.get("target") or ""),
                str(entry.get("run_id") or ""),
                _depth=depth + 1,
            )
        return False
