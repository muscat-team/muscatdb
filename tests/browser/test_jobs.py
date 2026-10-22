"""The /jobs page: rows, log viewer, re-run, delete, sorting and collapse state.

The status/type/instrument filter chips are covered by
tests/test_jobs_filter_chips_browser.py.
"""

from __future__ import annotations

from tests.browser.seed import RUN_ID

DONE_KEY = f"photometry:muscat3/261001/TOI-1000/{RUN_ID}"
ERROR_KEY = "photometry:muscat2/261002/HAT-P-1/r2"


def _row(page, key: str):
    return page.locator(f'tr.job-row[data-key="{key}"]')


def _targets(page, section: str) -> list[str]:
    return page.eval_on_selector_all(
        f'table[data-job-type="{section}"] tbody tr.job-row',
        "rs => rs.filter(r => r.style.display !== 'none').map(r => r.dataset.jobTarget)",
    )


def test_rows_land_in_their_sections(page):
    page.goto("/jobs")
    assert sorted(_targets(page, "photometry")) == ["HAT-P-1", "TOI-1000"]
    assert _targets(page, "transit_fit") == ["TOI-1000"]
    assert _row(page, ERROR_KEY).get_attribute("data-status") == "error"
    assert "pipeline failed" in _row(page, ERROR_KEY).inner_text()


def test_log_button_toggles_the_run_log(page):
    page.goto("/jobs")
    row = _row(page, DONE_KEY)
    log_row = row.locator("xpath=following-sibling::tr[1]")
    row.locator('[data-job-action="log"]').click()
    page.wait_for_function(
        "k => !document.querySelector(`tr.job-row[data-key=\"${k}\"]`).nextElementSibling"
        ".innerText.includes('Loading')",
        arg=DONE_KEY,
    )
    assert "photometry finished" in log_row.inner_text()
    row.locator('[data-job-action="log"]').click()
    assert log_row.is_hidden()


def test_rerun_launches_with_the_stored_options(page, monkeypatch):
    from muscat_db import photometry

    launches = []

    def start_run(inst, date, target, **kwargs):
        launches.append((inst, date, target, kwargs))
        return {"ok": True, "key": DONE_KEY, "run_id": RUN_ID}

    monkeypatch.setattr(photometry, "start_run", start_run)
    page.goto("/jobs")
    with page.expect_response("**/api/jobs/rerun") as resp:
        _row(page, DONE_KEY).locator('[data-job-action="rerun"]').click()
    assert resp.value.ok
    assert [(i, d, t) for i, d, t, _ in launches] == [("muscat3", "261001", "TOI-1000")]
    assert launches[0][3]["options"]["run_name"] == RUN_ID
    assert _row(page, DONE_KEY).get_attribute("data-status") == "running"


def test_rerun_failure_is_reported_and_the_button_recovers(page):
    # The harness refuses process launches, so the real start_run fails.
    page.problems.allowed += ["/api/jobs/rerun", "status of 400"]
    page.goto("/jobs")
    button = _row(page, ERROR_KEY).locator('[data-job-action="rerun"]')
    button.click()
    page.locator("#message-modal-ok").wait_for()
    assert page.inner_text("#message-modal-title") == "Error"
    assert page.inner_text("#message-modal-text").strip()
    page.click("#message-modal-ok")
    assert button.inner_text() == "⟳ Re-run" and button.is_enabled()
    assert _row(page, ERROR_KEY).get_attribute("data-status") == "error"


def test_delete_needs_confirmation(page, live_server):
    page.goto("/jobs")
    _row(page, ERROR_KEY).locator('[data-job-action="delete"]').click()
    page.locator("#hide-confirm-overlay").get_by_role("button", name="Cancel").click()
    assert _row(page, ERROR_KEY).count() == 1

    _row(page, ERROR_KEY).locator('[data-job-action="delete"]').click()
    with page.expect_response("**/api/jobs/delete"):
        page.click("#delete-confirm-btn")
    assert _row(page, ERROR_KEY).count() == 0
    assert live_server.sql("SELECT key FROM jobs WHERE key = ?", (ERROR_KEY,)) == []
    page.reload()
    assert _row(page, ERROR_KEY).count() == 0


def test_sort_and_collapsed_sections_survive_reload(page):
    page.goto("/jobs")
    header = page.locator('table[data-job-type="photometry"] th[data-sort-attr="target"]')
    header.click()
    first = _targets(page, "photometry")
    header.click()
    assert _targets(page, "photometry") == list(reversed(first))
    order = _targets(page, "photometry")

    page.locator("#transit-fit-jobs-section > summary").click()
    page.reload()
    page.wait_for_function("document.querySelector('#transit-fit-jobs-section').open === false")
    assert _targets(page, "photometry") == order
