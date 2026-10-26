"""Overwrite confirmation, authorship and the run audit trail (issues #307, #317).

Unit level: the suggested run name, the record a re-run writes, who the
visibility author rule follows after a re-run, the photometry overwrite clear
that keeps the record, and the ``run_events`` table. The routes that use them
are in ``tests/test_run_overwrite_routes.py``.
"""

from __future__ import annotations

import sqlite3

import pytest

from muscat_db import product_record as pr
from muscat_db import run_events
from muscat_db import run_overwrite as ro
from muscat_db.database import _APP_OWNED_TABLES, SCHEMA
from tests.test_product_visibility import DATE, INST, RESTRICTED, TARGET, _frames, _put, _viewer


def _never_taken(_run_id):
    return False


# ── suggested run name ──────────────────────────────────────────────────


def test_own_run_suggests_the_next_number():
    assert ro.suggest_run_name("photometry", "default", "alice", "alice", _never_taken) == "default_2"


def test_a_numbered_own_run_counts_on_instead_of_appending():
    assert ro.suggest_run_name("photometry", "default_2", "alice", "Alice", _never_taken) == "default_3"


def test_someone_elses_run_suggests_the_viewers_name():
    assert ro.suggest_run_name("photometry", "default", "bob", "alice", _never_taken) == "default_bob"


def test_suggestion_skips_names_already_taken():
    taken = {"default_bob", "default_bob_2"}
    assert ro.suggest_run_name("photometry", "default", "bob", "alice", taken.__contains__) == "default_bob_3"


def test_suggestion_keeps_the_site_prefix_of_a_multisite_run_id():
    asked = []

    def is_taken(run_id):
        asked.append(run_id)
        return False

    ro.suggest_run_name("photometry", "lsc-tel05-full_frame-night1", "alice", "alice", is_taken)
    assert asked == ["lsc-tel05-full_frame-night1_2"]


def test_suggestion_fits_the_run_name_length_cap():
    name = "x" * 40
    suggested = ro.suggest_run_name("transit_fit", name, "bob", "alice", _never_taken)
    assert len(suggested) <= 40 and suggested.endswith("_bob")


def test_no_suggestion_when_every_candidate_is_taken():
    assert ro.suggest_run_name("photometry", "default", "alice", "alice", lambda _r: True) is None


def test_ttv_run_ids_are_the_run_names():
    assert ro.run_name_of("ttv_fit", "My Run") == "my_run"
    assert ro.renamed_run_id("ttv_fit", "My Run", "my_run_2") == "my_run_2"


def test_confirm_token_names_run_and_owner_ignoring_case():
    assert ro.confirm_token("r1", "Alice") == ro.confirm_token("r1", "alice")
    assert ro.confirm_token("r1", "alice") != ro.confirm_token("r1", "bob")
    assert ro.confirm_token("r1", "alice") != ro.confirm_token("r2", "alice")


def test_confirm_token_changes_when_the_record_is_rewritten():
    v1 = "2026-10-01T00:00:00+00:00"
    assert ro.confirm_token("r1", "alice", v1) != ro.confirm_token("r1", "alice", "2026-10-20T00:00:00+00:00")


def test_record_version_follows_a_rerun_by_someone_else():
    prior = _record("alice", created_at="2026-10-01T00:00:00+00:00")
    rerun = pr.carry_authorship(_record("carol", created_at="2026-10-20T00:00:00+00:00"), prior)
    assert ro.record_version(prior) == "2026-10-01T00:00:00+00:00"
    assert rerun["author"] == "alice"  # so the owner alone could not tell the runs apart
    assert ro.record_version(rerun) == "2026-10-20T00:00:00+00:00"
    assert ro.record_version(None) == ""


def test_conflict_payload_says_whose_run_it_is():
    own = ro.conflict_payload(level="photometry", run_id="r1", viewer="alice", owner="alice",
                              created_at="2026-10-20T00:00:00+00:00", suggested="r1_2")
    other = ro.conflict_payload(level="photometry", run_id="r1", viewer="bob", owner="alice",
                                created_at="", suggested="r1_bob")
    assert own["conflict"] == "run_exists" and own["own_run"] is True and own["ok"] is False
    assert other["own_run"] is False and other["owner"] == "alice"
    assert "alice's run" in other["error"]
    assert other["confirm_token"] == ro.confirm_token("r1", "alice")
    assert other["can_overwrite"] is True


def test_hidden_conflict_payload_says_only_that_the_name_is_taken():
    body = ro.hidden_conflict_payload(level="photometry", run_id="r1", suggested="r1_bob")
    assert body["conflict"] == "run_exists" and body["can_overwrite"] is False
    assert (body["owner"], body["created_at"], body["suggested_run_name"]) == ("", "", "r1_bob")
    assert "confirm_token" not in body


# ── the record a re-run writes ──────────────────────────────────────────


def _record(author, level="photometry", created_at="2026-10-20T00:00:00+00:00"):
    return pr.build_record(level, author=author, inputs=[], created_at=created_at)


def test_rerun_by_someone_else_keeps_the_author():
    prior = _record("alice", created_at="2026-10-01T00:00:00+00:00")
    new = _record("carol", created_at="2026-10-20T00:00:00+00:00")

    out = pr.carry_authorship(new, prior)

    assert out["author"] == "alice"
    assert out["created_at"] == "2026-10-01T00:00:00+00:00"
    assert out["modified_by"] == "carol"
    assert out["modified_at"] == "2026-10-20T00:00:00+00:00"
    assert new["author"] == "carol" and "modified_by" not in new  # not mutated


def test_rerun_by_the_author_is_a_fresh_record():
    prior = {**_record("alice"), "modified_by": "carol", "modified_at": "x"}
    new = _record("Alice")
    assert pr.carry_authorship(new, prior) == new


@pytest.mark.parametrize("prior", [None, _record(""), _record("alice", level="transit_fit")])
def test_nothing_to_carry_from_a_missing_unowned_or_other_level_record(prior):
    new = _record("carol")
    assert pr.carry_authorship(new, prior) == new


def test_write_rerun_record_keeps_the_author_on_disk(tmp_path):
    assert pr.write_record(tmp_path, _record("alice"))
    assert pr.write_rerun_record(tmp_path, _record("carol"))
    on_disk = pr.read_record(tmp_path)
    assert (on_disk["author"], on_disk["modified_by"]) == ("alice", "carol")


# ── visibility follows the maker ────────────────────────────────────────


def _put_rerun(tmp_path, inputs, author, modified_by):
    _put(tmp_path, "photometry", inputs, author=author)
    rdir = tmp_path / "photometry" / INST / DATE / TARGET / "r1"
    record = pr.read_record(rdir)
    assert pr.write_record(rdir, {**record, "modified_by": modified_by, "modified_at": pr.now_iso()})


def test_rerunner_sees_the_run_they_made_by_the_author_rule(tmp_path):
    _put_rerun(tmp_path, [_frames(RESTRICTED)], author="alice", modified_by="carol")
    assert not _viewer(tmp_path, user="carol").hidden("photometry", INST, DATE, TARGET, "r1")


def test_author_does_not_see_inputs_a_rerunner_chose_by_the_author_rule(tmp_path):
    _put_rerun(tmp_path, [_frames(RESTRICTED)], author="alice", modified_by="carol")
    assert _viewer(tmp_path, user="alice").hidden("photometry", INST, DATE, TARGET, "r1")


# ── the photometry overwrite clear keeps the record ─────────────────────


@pytest.fixture
def phot_run(tmp_path, monkeypatch):
    from muscat_db import photometry as phot

    monkeypatch.setattr(phot, "results_dir", lambda inst, date: tmp_path / inst / date)
    rdir = phot.run_output_dir("muscat3", "261001", "TOI-1000", "r1")
    (rdir / "sub").mkdir(parents=True)
    (rdir / "TOI-1000_lc.csv").write_text("t,f\n")
    (rdir / "sub" / "x.png").write_bytes(b"png")
    assert pr.write_record(rdir, _record("alice"))
    return phot, rdir


def test_overwrite_clear_keeps_only_the_record(phot_run):
    phot, rdir = phot_run
    ok, _, err = phot._delete_reduction_files("muscat3", "261001", "TOI-1000", "r1", keep_record=True)
    assert ok and err is None
    assert sorted(p.name for p in rdir.iterdir()) == [pr.RECORD_NAME]
    assert pr.read_record(rdir)["author"] == "alice"


def test_delete_clear_removes_the_record_too(phot_run):
    phot, rdir = phot_run
    ok, _, _ = phot._delete_reduction_files("muscat3", "261001", "TOI-1000", "r1")
    assert ok and not rdir.exists()


# ── run_events ──────────────────────────────────────────────────────────


def test_run_events_is_app_owned_and_in_the_schema(tmp_path):
    assert "run_events" in _APP_OWNED_TABLES
    db = tmp_path / "m.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'run_events'").fetchone()


def test_run_events_records_and_reads_back_in_order(tmp_path):
    db = str(tmp_path / "m.db")
    sqlite3.connect(db).close()  # a database that predates the table
    for action in ("overwrite", "delete"):
        assert run_events.record(db, action=action, level="photometry", inst="muscat3", date="261001",
                                 target="TOI-1000", run_id="r1", actor="carol", owner="alice")
    rows = run_events.for_run(db, "photometry", "muscat3", "261001", "TOI-1000", "r1")
    assert [(r["action"], r["actor"], r["owner"]) for r in rows] == [
        ("overwrite", "carol", "alice"), ("delete", "carol", "alice"),
    ]


def test_run_events_reads_nothing_before_the_table_exists(tmp_path):
    db = str(tmp_path / "m.db")
    sqlite3.connect(db).close()
    assert run_events.for_run(db, "photometry", "", "", "T", "r1") == []


def test_run_events_rejects_an_unknown_action(tmp_path):
    with pytest.raises(ValueError):
        run_events.record(str(tmp_path / "m.db"), action="rename", level="photometry", inst="", date="",
                          target="T", run_id="r1", actor="a", owner="b")


def test_a_failed_audit_write_is_logged_not_raised(tmp_path, caplog):
    bad = str(tmp_path / "missing" / "m.db")  # the directory does not exist
    assert run_events.record(bad, action="delete", level="photometry", inst="", date="",
                             target="T", run_id="r1", actor="a", owner="b") is False
    assert "could not record delete" in caplog.text
