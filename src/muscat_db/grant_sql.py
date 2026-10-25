"""Compile many access grants into one SQL clause, by shape (issue #228).

A viewer's grants used to become one ``OR`` term each. SQLite refuses an
expression tree that deep ("Expression tree is too large") at about 1,000
grants, and each term cost ~1.3 ms per query on production's ``summaries``.

Grants are grouped by *shape*: which of proposal, instrument and target they
set, plus their date range. Each shape becomes one row-value membership test::

    (upper(COALESCE(proposal_id, '')), object) IN (VALUES (?, ?), (?, ?), ...)

so the clause grows with the number of distinct shapes, not grants. Values
are compared the way :meth:`muscat_db.access_groups.Grant.matches` compares
them: proposal ids upper-cased, instruments lower-cased, objects exactly. The
``OR`` of the shapes is built as a balanced tree, so its depth grows with the
logarithm of the shape count. Above :data:`MAX_GRANT_SHAPES` compilation
fails loudly instead of handing SQLite a query it may reject.

This module imports nothing from the project, so :mod:`muscat_db.database`
can use it without an import cycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

# Well inside SQLite's expression-depth limit (1000) even before the balanced
# OR; a viewer with this many distinct shapes needs a different grant design.
MAX_GRANT_SHAPES = 500


class GrantShapeLimitError(RuntimeError):
    """A viewer's grants need more than :data:`MAX_GRANT_SHAPES` clauses."""


class GrantFields(Protocol):
    proposal_id: str | None
    instrument: str | None
    target: str | None
    date_from: str | None
    date_to: str | None
    objects: frozenset[str] | None


def _columns(p: str, has_proposal: bool, has_instrument: bool, has_target: bool) -> list[str]:
    cols = []
    if has_proposal:
        cols.append(f"upper(COALESCE({p}proposal_id, ''))")
    if has_instrument:
        cols.append(f"lower({p}instrument)")
    if has_target:
        cols.append(f"{p}object")
    return cols


def _tuples(grant: GrantFields) -> list[tuple]:
    """The value tuples one grant contributes to its shape's ``IN`` list. A
    target grant whose target resolved to no object contributes none."""
    head = []
    if grant.proposal_id is not None:
        head.append(grant.proposal_id.upper())
    if grant.instrument is not None:
        head.append(grant.instrument.lower())
    if grant.target is None:
        return [tuple(head)]
    return [(*head, obj) for obj in sorted(grant.objects or ())]


def _membership(cols: list[str], values: list[tuple]) -> tuple[str, list]:
    params = [v for row in values for v in row]
    if len(cols) == 1:
        return f"{cols[0]} IN ({','.join('?' for _ in values)})", params
    row = "(" + ",".join("?" for _ in cols) + ")"
    return f"({', '.join(cols)}) IN (VALUES {','.join(row for _ in values)})", params


def _balanced_or(clauses: list[str]) -> str:
    if len(clauses) == 1:
        return clauses[0]
    mid = len(clauses) // 2
    return f"({_balanced_or(clauses[:mid])} OR {_balanced_or(clauses[mid:])})"


def compile_grants(grants: Iterable[GrantFields], alias: str = "") -> tuple[str, list]:
    """``(clause, params)`` true for a row any of *grants* matches, over the
    ``proposal_id``/``instrument``/``object``/``obsdate`` columns of
    ``summaries`` or ``frames`` (qualified by *alias* when given).

    ``("0", [])`` when no grant can match anything. Raises
    :class:`GrantShapeLimitError` above :data:`MAX_GRANT_SHAPES` shapes.
    """
    p = f"{alias}." if alias else ""
    shapes: dict[tuple, set[tuple]] = {}
    for g in grants:
        key = (
            g.proposal_id is not None,
            g.instrument is not None,
            g.target is not None,
            g.date_from,
            g.date_to,
        )
        shapes.setdefault(key, set()).update(_tuples(g))
    clauses: list[str] = []
    params: list = []
    for (has_p, has_i, has_t, date_from, date_to), values in sorted(
        shapes.items(), key=lambda kv: tuple("" if v is None else str(v) for v in kv[0])
    ):
        parts: list[str] = []
        part_params: list = []
        cols = _columns(p, has_p, has_i, has_t)
        if cols:
            if not values:
                continue  # every target grant of this shape resolved to nothing
            clause, vals = _membership(cols, sorted(values))
            parts.append(clause)
            part_params.extend(vals)
        if date_from is not None:
            parts.append(f"{p}obsdate >= ?")
            part_params.append(date_from)
        if date_to is not None:
            parts.append(f"{p}obsdate <= ?")
            part_params.append(date_to)
        if not parts:
            continue
        clauses.append("(" + " AND ".join(parts) + ")" if len(parts) > 1 else parts[0])
        params.extend(part_params)
    if len(clauses) > MAX_GRANT_SHAPES:
        raise GrantShapeLimitError(
            f"{len(clauses)} distinct grant shapes exceed the limit of {MAX_GRANT_SHAPES}; "
            "merge date ranges or grant by proposal instead of by many separate fields"
        )
    if not clauses:
        return "0", []
    return _balanced_or(clauses), params
