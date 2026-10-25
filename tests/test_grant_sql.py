"""Grants compiled to SQL by shape (issue #228, grant_sql.py)."""

from __future__ import annotations

import itertools
import random
import sqlite3

import pytest

from muscat_db.access_groups import Access, Grant
from muscat_db.database import ScopedDenied, sql_not_denied
from muscat_db.grant_sql import MAX_GRANT_SHAPES, GrantShapeLimitError, compile_grants

# (instrument, obsdate, object, proposal_id), with the header-case variety the
# catalog really has: proposal ids and instruments are matched case-insensitively.
ROWS = [
    (inst, date, obj, pid)
    for inst in ("sinistro", "QHY600", "muscat2")
    for date in ("260101", "260115", "260131")
    for obj in ("TOI-1234", "TOI 01234.01", "HIP 67522", "OTHER")
    for pid in ("CON-002", "con-002", "KEY-001", "")
]


def _table() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE summaries (instrument TEXT, obsdate TEXT, object TEXT, proposal_id TEXT)")
    conn.executemany("INSERT INTO summaries VALUES (?, ?, ?, ?)", ROWS)
    return conn


def _sql_rows(conn, clause, params, alias=""):
    table = f"summaries {alias}" if alias else "summaries"
    return set(
        conn.execute(
            f"SELECT instrument, obsdate, object, proposal_id FROM {table} WHERE {clause}", params
        )
    )


def _grant_variety() -> list[Grant]:
    targets = {
        None: None,
        "TOI-1234": frozenset({"TOI-1234", "TOI 01234.01"}),
        "HIP67522": frozenset({"HIP 67522"}),
        "NOSUCH": frozenset(),  # resolved to no object: matches nothing
    }
    grants = []
    for pid, inst, target, dates in itertools.product(
        (None, "CON-002", "key-001", ""),
        (None, "sinistro", "qhy600"),
        targets,
        (None, ("260101", "260115"), ("260131", "260131")),
    ):
        if pid is None and inst is None and target is None and dates is None:
            continue  # the schema refuses a wildcard grant
        grants.append(
            Grant(
                proposal_id=pid,
                instrument=inst,
                target=target,
                date_from=dates[0] if dates else None,
                date_to=dates[1] if dates else None,
                objects=targets[target],
            )
        )
    return grants


@pytest.mark.parametrize("seed", range(25))
def test_compiled_sql_matches_exactly_what_the_grants_match(seed):
    grants = random.Random(seed).sample(_grant_variety(), k=random.Random(seed).randint(1, 12))
    acc = Access("viewer", False, frozenset({"g"}), tuple(grants))
    clause, params = acc.sql_visible()
    assert _sql_rows(_table(), clause, params) == {r for r in ROWS if acc.frame_visible(*r)}


def test_scoped_sql_with_an_alias_agrees_with_the_rules():
    grants = tuple(g for g in _grant_variety() if g.proposal_id != "CON-002")
    denied = ScopedDenied({"", "KEY-001", "CON-002"}, {"CON-002"}, grants)
    clause, params = sql_not_denied(denied, "s.proposal_id")
    expected = {
        r for r in ROWS
        if r[3].upper() == "CON-002" or any(g.matches(*r) for g in grants)
    }
    assert _sql_rows(_table(), clause, params, alias="s") == expected


def _many_target_grants(n: int) -> tuple[Grant, ...]:
    return tuple(
        Grant(proposal_id="KEY-001", target=f"T{i}", objects=frozenset({f"T{i}"})) for i in range(n)
    )


def test_a_thousand_target_grants_compile_into_one_clause_sqlite_accepts():
    # One OR term per grant made SQLite refuse this query ("Expression tree is
    # too large (maximum depth 1000)"), so the viewer's pages failed outright.
    denied = ScopedDenied({"", "KEY-001"}, frozenset(), _many_target_grants(1000))
    clause, params = sql_not_denied(denied)
    assert " OR " not in clause
    conn = _table()
    conn.executemany(
        "INSERT INTO summaries VALUES ('sinistro', '260101', ?, 'KEY-001')",
        [(f"T{i}",) for i in range(0, 1000, 7)],
    )
    assert len(_sql_rows(conn, clause, params)) == len(range(0, 1000, 7))


def test_the_shape_limit_fails_loudly_rather_than_building_the_query():
    def dated(n):
        # n single-night grants, each its own shape: a date range cannot share
        # an IN list. Distinct six-digit strings stand in for obsdates.
        return [Grant(instrument="sinistro", date_from=f"{200000 + i}", date_to=f"{200000 + i}") for i in range(n)]

    clause, params = compile_grants(dated(MAX_GRANT_SHAPES))
    assert clause.count(" OR ") == MAX_GRANT_SHAPES - 1
    _sql_rows(_table(), clause, params)  # SQLite accepts the query at the limit
    with pytest.raises(GrantShapeLimitError, match="exceed the limit"):
        compile_grants(dated(MAX_GRANT_SHAPES + 1))


def test_grants_resolving_to_no_object_compile_to_false():
    assert compile_grants([Grant(target="NOSUCH", objects=frozenset())]) == ("0", [])
    assert Access("a", False, frozenset()).sql_visible() == ("0", [])
    assert Access("a", True, frozenset()).sql_visible() == ("1", [])
