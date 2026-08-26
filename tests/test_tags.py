"""Tests for the target tagging / project management feature (issue #88).

target_tags keys on the normalized target identity (norm_name), not the raw
obslog OBJECT string -- see the "Key design decision" note in
notes/target_tagging_plan.md. The core regression test below pins that down:
two raw objects that normalize to the same target must share one project's
membership.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from muscat_db.database import (
    add_target_tag,
    create_tag,
    delete_tag,
    get_tag_description,
    get_tags_for_targets,
    get_targets_for_tag,
    list_project_tags,
    remove_target_tag,
    rename_tag,
    set_norm_name_override,
    set_tag_description,
)
from muscat_db.web import app, _project_target_rows, _render_markdown


# ── DB layer ─────────────────────────────────────────────────────────────────


def test_create_tag_and_duplicate_rejected_case_insensitively(mock_db):
    assert create_tag(mock_db, "FollowUp", "Interesting targets") is True
    assert create_tag(mock_db, "followup") is False


def test_list_project_tags_includes_zero_target_projects(mock_db):
    create_tag(mock_db, "Empty Project", "no targets yet")

    tags = list_project_tags(mock_db)

    assert tags == [{"tag": "Empty Project", "description": "no targets yet", "target_count": 0}]


def test_add_target_tag_rejects_unknown_project(mock_db):
    assert add_target_tag(mock_db, "TESTOBJ", "NoSuchProject") is False
    assert get_targets_for_tag(mock_db, "NoSuchProject") == []


def test_add_target_tag_is_idempotent_and_updates_count(mock_db):
    create_tag(mock_db, "FollowUp")

    assert add_target_tag(mock_db, "TESTOBJ", "FollowUp") is True
    assert add_target_tag(mock_db, "TESTOBJ", "FollowUp") is True  # re-tag, no error

    tags = list_project_tags(mock_db)
    assert tags == [{"tag": "FollowUp", "description": "", "target_count": 1}]
    assert get_targets_for_tag(mock_db, "FollowUp") == ["TESTOBJ"]


def test_remove_target_tag_is_idempotent(mock_db):
    create_tag(mock_db, "FollowUp")
    add_target_tag(mock_db, "TESTOBJ", "FollowUp")

    remove_target_tag(mock_db, "TESTOBJ", "FollowUp")
    remove_target_tag(mock_db, "TESTOBJ", "FollowUp")  # already gone, no error

    assert get_targets_for_tag(mock_db, "FollowUp") == []
    # The project itself survives detaching its only target.
    assert list_project_tags(mock_db) == [{"tag": "FollowUp", "description": "", "target_count": 0}]


def test_get_tags_for_targets_batches_multiple_targets(mock_db):
    create_tag(mock_db, "A")
    create_tag(mock_db, "B")
    add_target_tag(mock_db, "OBJ1", "A")
    add_target_tag(mock_db, "OBJ1", "B")
    add_target_tag(mock_db, "OBJ2", "A")

    result = get_tags_for_targets(mock_db, ["OBJ1", "OBJ2", "OBJ3"])

    assert result == {"OBJ1": ["A", "B"], "OBJ2": ["A"]}


def test_tag_description_distinguishes_missing_from_blank(mock_db):
    assert get_tag_description(mock_db, "Ghost") is None

    create_tag(mock_db, "Blank")
    assert get_tag_description(mock_db, "Blank") == ""

    assert set_tag_description(mock_db, "Blank", "now described") is True
    assert get_tag_description(mock_db, "Blank") == "now described"

    assert set_tag_description(mock_db, "Ghost", "x") is False


def test_delete_tag_cascades_to_target_tags(mock_db):
    create_tag(mock_db, "Temp")
    add_target_tag(mock_db, "TESTOBJ", "Temp")

    delete_tag(mock_db, "Temp")

    assert get_tag_description(mock_db, "Temp") is None
    assert get_targets_for_tag(mock_db, "Temp") == []


def test_rename_tag_moves_description_and_target_tags(mock_db):
    create_tag(mock_db, "Old", "a description")
    add_target_tag(mock_db, "TESTOBJ", "Old")

    result = rename_tag(mock_db, "Old", "New")

    assert result == "New"
    assert get_tag_description(mock_db, "Old") is None
    assert get_tag_description(mock_db, "New") == "a description"
    assert get_targets_for_tag(mock_db, "Old") == []
    assert get_targets_for_tag(mock_db, "New") == ["TESTOBJ"]


def test_rename_tag_rejects_unknown_source(mock_db):
    assert rename_tag(mock_db, "Ghost", "New") is None


def test_rename_tag_conflicts_with_existing_different_project(mock_db):
    create_tag(mock_db, "A")
    create_tag(mock_db, "B")

    assert rename_tag(mock_db, "A", "B") == "conflict"


def test_rename_tag_allows_case_only_change(mock_db):
    create_tag(mock_db, "FollowUp")

    assert rename_tag(mock_db, "FollowUp", "followup") == "followup"
    assert get_tag_description(mock_db, "followup") == ""


# ── Core design regression: norm_name groups raw-object spelling variants ────


def test_project_target_rows_aggregates_raw_objects_sharing_a_norm_name(mock_db, monkeypatch):
    create_tag(mock_db, "Grouped")
    add_target_tag(mock_db, "GROUPED", "Grouped")
    set_norm_name_override(mock_db, "obj-a", "GROUPED")
    set_norm_name_override(mock_db, "obj-b", "GROUPED")

    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [
            {
                "object": "obj-a", "n_dates": 1, "n_frames": 10,
                "dates": ["260101"], "date_to_inst": {"260101": "muscat3"},
                "filters": ["g"], "instruments": ["muscat3"],
            },
            {
                "object": "obj-b", "n_dates": 1, "n_frames": 5,
                "dates": ["260102"], "date_to_inst": {"260102": "muscat4"},
                "filters": ["r"], "instruments": ["muscat4"],
            },
        ],
    )

    rows = _project_target_rows(mock_db, "Grouped")

    assert len(rows) == 1
    row = rows[0]
    assert row["norm_name"] == "GROUPED"
    assert row["objects"] == ["obj-a", "obj-b"]
    assert row["dates"] == ["260101", "260102"]
    assert row["n_dates"] == 2
    assert row["n_frames"] == 15
    assert row["filters"] == ["g", "r"]
    assert row["instruments"] == ["muscat3", "muscat4"]


def test_project_target_rows_includes_per_date_frame_counts(mock_db, monkeypatch):
    create_tag(mock_db, "Grouped")
    add_target_tag(mock_db, "GROUPED", "Grouped")
    set_norm_name_override(mock_db, "obj-a", "GROUPED")
    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [{
            "object": "obj-a", "n_dates": 2, "n_frames": 150,
            "dates": ["260101", "260102"],
            "date_to_inst": {"260101": "muscat3", "260102": "muscat3"},
            "filters": ["g"], "instruments": ["muscat3"],
        }],
    )
    with sqlite3.connect(mock_db) as conn:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, nframes, filter) VALUES (?,?,?,?,?,?)",
            ("muscat3", "260101", 0, "obj-a", 40, "g"),
        )
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, nframes, filter) VALUES (?,?,?,?,?,?)",
            ("muscat3", "260102", 0, "obj-a", 110, "r"),
        )
        conn.commit()

    rows = _project_target_rows(mock_db, "Grouped")

    assert rows[0]["date_frames"] == {"260101": 40, "260102": 110}
    assert [c["label"] for c in rows[0]["date_filter_chips"]["260101"]] == ["g"]
    assert [c["label"] for c in rows[0]["date_filter_chips"]["260102"]] == ["r"]


# ── Homepage: Tags column present, RA/Dec/Airmass/Move gone ──────────────────


def _seed_one_target(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [{
            "object": "TESTOBJ",
            "is_identified": True,
            "ra": "04:05:23.4940",
            "declination": "+20:11:36.595",
            "filters": ["g"],
            "filter_chips": [{"label": "g", "color": "g", "narrow": False}],
            "n_frames": 10,
            "n_dates": 1,
            "airmass_min": 1.1,
            "airmass_max": 1.4,
            "instruments": ["muscat3"],
            "dates": ["260101"],
            "date_to_inst": {"260101": "muscat3"},
            "note": "",
            "total_exptime_hr": 1.0,
        }],
    )


def test_homepage_drops_ra_dec_airmass_move_and_shows_tags(mock_db, monkeypatch):
    _seed_one_target(monkeypatch)
    create_tag(mock_db, "FollowUp")
    add_target_tag(mock_db, "TESTOBJ", "FollowUp")

    html = TestClient(app).get("/targets").text

    assert ">Tags<" in html
    assert 'data-tags="FollowUp"' in html
    assert 'href="/tag?name=FollowUp"' in html
    assert ">RA<" not in html
    assert ">Dec<" not in html
    assert ">Airmass<" not in html
    assert ">Move<" not in html
    assert "btn-move" not in html


def test_homepage_tags_are_not_stale_after_mutation(mock_db, monkeypatch):
    """Regression for the _index_cache staleness risk: a tag mutation commits
    through get_conn, which changes _db_mtime, which is part of index()'s
    cache key -- so a second GET must see the freshly attached tag, not the
    HTML cached by the first GET."""
    _seed_one_target(monkeypatch)
    create_tag(mock_db, "FollowUp")
    client = TestClient(app)

    first = client.get("/targets").text
    assert 'data-tags="FollowUp"' not in first

    add_target_tag(mock_db, "TESTOBJ", "FollowUp")

    second = client.get("/targets").text
    assert 'data-tags="FollowUp"' in second


# ── API ──────────────────────────────────────────────────────────────────────


def test_api_create_tag_then_conflict_on_duplicate(mock_db):
    client = TestClient(app)

    created = client.post("/api/tags", json={"tag": "FollowUp"})
    assert created.status_code == 200
    assert created.json() == {"ok": True, "tag": "FollowUp"}

    dup = client.post("/api/tags", json={"tag": "FollowUp"})
    assert dup.status_code == 409


def test_api_create_tag_rejects_empty_and_slash(mock_db):
    client = TestClient(app)

    assert client.post("/api/tags", json={"tag": ""}).status_code == 400
    assert client.post("/api/tags", json={"tag": "a/b"}).status_code == 400


def test_api_attach_and_detach_target_tag(mock_db, monkeypatch):
    _seed_one_target(monkeypatch)
    client = TestClient(app)
    client.post("/api/tags", json={"tag": "FollowUp"})

    attached = client.put("/api/targets/TESTOBJ/tags", json={"tag": "FollowUp"})
    assert attached.status_code == 200
    assert attached.json()["tag"] == "FollowUp"

    listed = client.get("/api/targets/TESTOBJ/tags").json()
    assert listed["tags"] == ["FollowUp"]

    detached = client.request("DELETE", "/api/targets/TESTOBJ/tags", json={"tag": "FollowUp"})
    assert detached.status_code == 200

    listed_after = client.get("/api/targets/TESTOBJ/tags").json()
    assert listed_after["tags"] == []


def test_api_attach_to_unknown_project_is_404(mock_db, monkeypatch):
    _seed_one_target(monkeypatch)
    client = TestClient(app)

    resp = client.put("/api/targets/TESTOBJ/tags", json={"tag": "NoSuchProject"})

    assert resp.status_code == 404


def test_api_attach_unknown_target_is_404_and_does_not_change_count(mock_db, monkeypatch):
    """A typo in the Project Detail page's attach box (a <datalist>, not a
    hard constraint) must not create a phantom target_tags row: the target
    must already exist as a real norm_name, not just be accepted as text."""
    _seed_one_target(monkeypatch)
    client = TestClient(app)
    client.post("/api/tags", json={"tag": "FollowUp"})

    resp = client.put("/api/targets/NOTREAL/tags", json={"tag": "FollowUp"})

    assert resp.status_code == 404
    tags = client.get("/api/tags").json()["tags"]
    assert tags == [{"tag": "FollowUp", "description": "", "target_count": 0}]


def test_api_update_description_unknown_tag_is_404(mock_db):
    client = TestClient(app)

    resp = client.put("/api/tags/NoSuchProject", json={"description": "x"})

    assert resp.status_code == 404


def test_api_update_description_success(mock_db):
    client = TestClient(app)
    client.post("/api/tags", json={"tag": "FollowUp"})

    resp = client.put("/api/tags/FollowUp", json={"description": "new description"})

    assert resp.status_code == 200
    assert resp.json()["description"] == "new description"
    assert client.get("/api/tags").json()["tags"][0]["description"] == "new description"


def test_api_delete_project(mock_db):
    client = TestClient(app)
    client.post("/api/tags", json={"tag": "FollowUp"})

    resp = client.delete("/api/tags/FollowUp")

    assert resp.status_code == 200
    assert client.get("/api/tags").json()["tags"] == []


def test_api_rename_tag_success(mock_db):
    client = TestClient(app)
    client.post("/api/tags", json={"tag": "Old"})

    resp = client.put("/api/tags/Old/rename", json={"new_tag": "New"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "tag": "New"}
    tags = [t["tag"] for t in client.get("/api/tags").json()["tags"]]
    assert tags == ["New"]


def test_api_rename_tag_conflict(mock_db):
    client = TestClient(app)
    client.post("/api/tags", json={"tag": "A"})
    client.post("/api/tags", json={"tag": "B"})

    resp = client.put("/api/tags/A/rename", json={"new_tag": "B"})

    assert resp.status_code == 409


def test_api_rename_tag_unknown_source_is_404(mock_db):
    resp = TestClient(app).put("/api/tags/Ghost/rename", json={"new_tag": "New"})

    assert resp.status_code == 404


def test_api_rename_tag_rejects_empty_and_slash(mock_db):
    client = TestClient(app)
    client.post("/api/tags", json={"tag": "Old"})

    assert client.put("/api/tags/Old/rename", json={"new_tag": ""}).status_code == 400
    assert client.put("/api/tags/Old/rename", json={"new_tag": "a/b"}).status_code == 400


def test_api_targets_export_csv_has_tags_column(mock_db, monkeypatch):
    _seed_one_target(monkeypatch)
    create_tag(mock_db, "FollowUp")
    add_target_tag(mock_db, "TESTOBJ", "FollowUp")

    resp = TestClient(app).get("/api/targets/export.csv")

    assert resp.status_code == 200
    header, row = resp.text.splitlines()[:2]
    assert header.strip().endswith(",tags")
    assert row.strip().endswith(",FollowUp")


def test_api_tag_export_csv(mock_db, monkeypatch):
    create_tag(mock_db, "FollowUp")
    add_target_tag(mock_db, "TESTOBJ", "FollowUp")
    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [{
            "object": "TESTOBJ", "n_dates": 1, "n_frames": 10,
            "dates": ["260101"], "date_to_inst": {"260101": "muscat3"},
            "filters": ["g"], "instruments": ["muscat3"],
        }],
    )

    resp = TestClient(app).get("/api/tags/FollowUp/export.csv")

    assert resp.status_code == 200
    assert "norm_name,dates,n_dates,filters,n_frames" in resp.text
    assert "TESTOBJ" in resp.text


def test_api_tag_export_csv_unknown_project_is_404(mock_db):
    resp = TestClient(app).get("/api/tags/NoSuchProject/export.csv")

    assert resp.status_code == 404


# ── Pages ────────────────────────────────────────────────────────────────────


def test_projects_directory_renders_cards(mock_db):
    create_tag(mock_db, "FollowUp", "Interesting targets")

    html = TestClient(app).get("/projects").text

    assert "FollowUp" in html
    assert "Interesting targets" in html
    assert 'href="/tag?name=FollowUp"' in html


def test_projects_directory_empty_state(mock_db):
    html = TestClient(app).get("/projects").text

    assert "No projects yet" in html


def test_tag_page_renders_attached_targets(mock_db, monkeypatch):
    create_tag(mock_db, "FollowUp", "desc here")
    add_target_tag(mock_db, "TESTOBJ", "FollowUp")
    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [{
            "object": "TESTOBJ", "n_dates": 1, "n_frames": 10,
            "dates": ["260101"], "date_to_inst": {"260101": "muscat3"},
            "filters": ["g"], "instruments": ["muscat3"],
        }],
    )

    resp = TestClient(app).get("/tag?name=FollowUp")

    assert resp.status_code == 200
    html = resp.text
    assert "TESTOBJ" in html
    assert "desc here" in html
    assert 'from=project&amp;project=FollowUp' in html or 'from=project&project=FollowUp' in html


def test_tag_page_dates_column_shows_instrument_label(mock_db, monkeypatch):
    create_tag(mock_db, "FollowUp")
    add_target_tag(mock_db, "TESTOBJ", "FollowUp")
    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [{
            "object": "TESTOBJ", "n_dates": 1, "n_frames": 10,
            "dates": ["260101"], "date_to_inst": {"260101": "muscat3"},
            "filters": ["g"], "instruments": ["muscat3"],
        }],
    )

    html = TestClient(app).get("/tag?name=FollowUp").text

    assert "260101(M3)" in html


def test_tag_page_uses_normalized_target_header(mock_db, monkeypatch):
    create_tag(mock_db, "FollowUp")
    add_target_tag(mock_db, "TESTOBJ", "FollowUp")
    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [{
            "object": "TESTOBJ", "n_dates": 1, "n_frames": 10,
            "dates": ["260101"], "date_to_inst": {"260101": "muscat3"},
            "filters": ["g"], "instruments": ["muscat3"],
        }],
    )

    html = TestClient(app).get("/tag?name=FollowUp").text

    assert '<th data-sort-attr="normName">Normalized Target</th>' in html


def test_tag_page_has_rename_control(mock_db):
    create_tag(mock_db, "FollowUp")

    html = TestClient(app).get("/tag?name=FollowUp").text

    assert 'id="project-name-cell"' in html
    assert 'data-tag="FollowUp"' in html
    assert 'class="project-name-text">FollowUp<' in html


def test_tag_page_renders_nframe_filter_and_per_date_frames(mock_db, monkeypatch):
    create_tag(mock_db, "FollowUp")
    add_target_tag(mock_db, "TESTOBJ", "FollowUp")
    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [{
            "object": "TESTOBJ", "n_dates": 1, "n_frames": 10,
            "dates": ["260101"], "date_to_inst": {"260101": "muscat3"},
            "filters": ["g"], "instruments": ["muscat3"],
        }],
    )

    html = TestClient(app).get("/tag?name=FollowUp").text

    assert 'id="nframe-filter"' in html
    assert 'value="0"' in html
    assert 'data-frames="0"' in html  # no summaries rows seeded -> defaults to 0
    assert 'ndataset-cell' in html
    assert 'class="filters-cell"' in html
    assert 'class="date-filter-chips"' in html
    assert '# Frames' not in html
    assert 'frames-cell' not in html


# ── Markdown description rendering ───────────────────────────────────────────


def test_render_markdown_renders_links_and_bullets():
    html = _render_markdown("[link](https://example.com)\n\n- one\n- two")

    assert '<a href="https://example.com"' in html
    assert "<li>one</li>" in html
    assert "<li>two</li>" in html


def test_render_markdown_strips_script_and_javascript_urls():
    html = _render_markdown("<script>alert(1)</script>[bad](javascript:alert(1)) safe")

    assert "<script>" not in html
    assert "alert(1)" not in html
    assert "javascript:" not in html
    assert "safe" in html


def test_tag_page_renders_description_as_sanitized_html(mock_db):
    create_tag(mock_db, "FollowUp", "[link](https://example.com) and\n\n- a bullet")

    html = TestClient(app).get("/tag?name=FollowUp").text

    assert '<a href="https://example.com"' in html
    assert "<li>a bullet</li>" in html
    # The raw markdown source survives (for re-editing), not just its render.
    assert 'data-raw="[link](https://example.com) and' in html


def test_tag_page_unknown_project_renders_friendly_not_found(mock_db):
    resp = TestClient(app).get("/tag?name=NoSuchProject")

    assert resp.status_code == 200
    assert "Project not found" in resp.text


def test_tag_page_empty_name_redirects_to_projects(mock_db):
    resp = TestClient(app).get("/tag", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"


def test_tags_alias_redirects_to_projects(mock_db):
    resp = TestClient(app).get("/tags", follow_redirects=False)

    assert resp.status_code == 301
    assert resp.headers["location"] == "/projects"
