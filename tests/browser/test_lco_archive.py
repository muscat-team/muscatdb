"""The /lco/archive search: coordinate match, dataset grouping, selection, restore."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect
import pytest


def _frame(day_obs: str, date_obs: str, seq: str) -> dict:
    base = f"lsc1m005-fa15-{day_obs}-{seq}-e91"
    return {
        "id": int(seq),
        "filename": f"{base}.fits.fz",
        "basename": base,
        "SITEID": "lsc",
        "TELID": "1m0a",
        "INSTRUME": "fa15",
        "FILTER": "ip",
        "RLEVEL": 91,
        "DATE_OBS": date_obs,
        "DAY_OBS": day_obs,
        "OBJECT": "TOI-1000",
        "proposal_id": "LCO2026B-002",
        "url": f"https://archive.invalid/{base}",
    }


# The first night is the fixture's sinistro 261003 (seed.FRAMES); the second is new.
ARCHIVE_FRAMES = [
    _frame("20261003", "2026-10-04T02:24:00.000Z", "0010"),
    _frame("20261009", "2026-10-10T02:24:00.000Z", "0001"),
]


@pytest.fixture
def archive(monkeypatch):
    """Answer the LCO archive's frames/ query; record every request it receives."""
    from muscat_db import lco

    requests = []

    def lco_api_request(
        url, method="GET", data=None, user_name=None, token=None, require_own_token=False
    ):
        requests.append(url)
        assert url.startswith("https://archive-api.lco.global/frames/"), url
        return {"count": len(ARCHIVE_FRAMES), "next": None, "results": ARCHIVE_FRAMES}

    monkeypatch.setattr(lco, "_lco_api_request", lco_api_request)
    return requests


def _search(page) -> None:
    page.goto("/lco/archive")
    page.fill("#dl-object", "TOI-1000")
    with page.expect_response("**/api/lco/archive/frames?**"):
        page.click("#btn-search")


def test_search_resolves_the_target_and_groups_frames_by_night(page, archive):
    _search(page)
    expect(page.locator("#msg-search")).to_have_text(
        "Showing 2 frames across 2 datasets — resolved to RA 117.929, Dec 12.536 (toi)"
    )
    (url,) = archive
    query = parse_qs(urlparse(url).query)
    assert query["covers"] == ["POINT(117.929 12.536)"]
    assert query["OBSTYPE"] == ["EXPOSE"]

    known, new = page.locator("#frames-groups details.lco-dataset").all()
    expect(known.locator("summary")).to_contain_text("Dataset date: 2026-10-03")
    expect(known.locator("summary")).to_contain_text("already in muscat-db (1 frame)")
    expect(new.locator("summary")).to_contain_text("Dataset date: 2026-10-09")
    expect(new.locator("summary")).to_contain_text("not in muscat-db")
    expect(page.locator("#btn-download")).to_be_enabled()


def test_group_checkbox_selects_its_night_and_selection_survives_a_reload(page, archive):
    _search(page)
    new = page.locator("#frames-groups details.lco-dataset").nth(1)
    new.locator(".frame-group-all").check()
    checked = page.locator("#frames-groups tbody input[type=checkbox]:checked")
    expect(checked).to_have_count(1)

    page.reload()
    expect(page.locator("#frames-groups details.lco-dataset")).to_have_count(2)
    expect(checked).to_have_count(1)
    row = checked.locator("xpath=ancestor::tr")
    expect(row).to_contain_text("lsc1m005-fa15-20261009-0001-e91")
    assert len(archive) == 1  # restored from the browser, not searched again


def test_search_without_a_target_explains_what_is_needed(page):
    page.problems.allowed.append("HTTP 400: GET /api/lco/archive/frames")
    page.problems.allowed.append("status of 400")
    page.goto("/lco/archive")
    page.fill("#dl-object", "")
    page.click("#btn-search")
    expect(page.locator("#msg-search")).to_have_text(
        "Enter a target name to resolve its coordinates, or enable 'Fuzzy name match'."
    )
