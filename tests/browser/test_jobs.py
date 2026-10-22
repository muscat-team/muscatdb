"""The /jobs page: rows, log viewer, re-run, delete, sorting and collapse state.

Includes the status/type/instrument filter chips and live job transitions.
"""

from __future__ import annotations

from playwright.sync_api import expect
import pytest

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


def test_rerun_failure_is_reported_and_the_button_recovers(page, monkeypatch):
    from muscat_db import photometry
    monkeypatch.setattr(photometry, "start_run", lambda *a, **k: {"ok": False, "error": "fixture launcher unavailable"})
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


def test_filter_chips_combine_clear_and_restore_after_reload(page):
    page.goto('/jobs')
    page.locator('.job-count[data-status="done"]').click()
    page.locator('.job-count[data-type="photometry"]').click()
    page.locator('.job-count[data-inst="muscat3"]').click()
    visible = page.locator('tr.job-row:visible')
    expect(visible).to_have_count(1)
    expect(visible).to_have_attribute('data-key', DONE_KEY)
    page.reload()
    expect(visible).to_have_count(1)
    expect(visible).to_have_attribute('data-key', DONE_KEY)
    page.click('#clear-filter')
    expect(page.locator('tr.job-row:visible')).to_have_count(3)
    page.fill('#job-search', 'HAT-P-1')
    expect(page.locator('tr.job-row:visible')).to_have_count(1)
    expect(page.locator('tr.job-row:visible')).to_have_attribute('data-key', ERROR_KEY)


@pytest.fixture
def active_job(live_server, monkeypatch):
    from muscat_db import photometry, transit_fit, ttv_fit
    for engine in (photometry, transit_fit, ttv_fit):
        monkeypatch.setattr(engine, 'sync_jobs', lambda: None)
    live_server.sql("UPDATE jobs SET state = 'running', returncode = NULL WHERE key = ?", (DONE_KEY,))
    return DONE_KEY


def test_cancel_running_job_then_receive_terminal_state(page, active_job, monkeypatch):
    from muscat_db import photometry
    calls = []
    monkeypatch.setattr(photometry, 'cancel_run', lambda *a, **k: calls.append((a, k)) or {'ok': True})
    # Drive only the status endpoint response, as a worker would update it.
    state = {'finished': {}, 'running': {}, 'counts': {}}
    page.route('**/api/jobs/status', lambda r: r.fulfill(json=state))
    page.goto('/jobs')
    row = _row(page, active_job)
    button = row.locator('[data-job-action="cancel"]')
    with page.expect_response('**/api/photometry/cancel'):
        button.click()
    expect(row).to_have_attribute('data-status', 'cancelling')
    expect(button).to_be_disabled()
    assert calls[-1][1]['run_id'] == RUN_ID
    state['finished'][active_job] = {'state': 'cancelled', 'elapsed': 42}
    expect(row).to_have_attribute('data-status', 'cancelled', timeout=15000)
    expect(row.locator('[data-job-action="rerun"]')).to_be_visible()
    expect(row.locator('[data-job-action="cancel"]')).to_have_count(0)


def test_cancel_failure_restores_the_button(page, active_job):
    page.problems.allowed += ['HTTP 503: POST /api/photometry/cancel', 'status of 503']
    page.route('**/api/photometry/cancel', lambda r: r.fulfill(status=503, json={'error': 'worker unreachable'}))
    page.goto('/jobs')
    button = _row(page, active_job).locator('[data-job-action="cancel"]')
    button.click()
    expect(page.locator('#message-modal-text')).to_contain_text('worker unreachable')
    page.click('#message-modal-ok')
    expect(button).to_be_enabled()
    expect(button).to_have_text('Cancel')


def test_other_user_cannot_cancel_or_delete_an_admins_jobs(page, active_job, live_server):
    from tests.browser.conftest import PROXY_SECRET
    live_server.sql("INSERT INTO users (username, display_name, is_admin) VALUES ('viewer', 'Viewer', 0)")
    page.context.set_extra_http_headers({'X-Forwarded-User': 'viewer', 'X-MuSCAT-Proxy-Secret': PROXY_SECRET})
    page.goto('/jobs')
    row = _row(page, active_job)
    expect(row.get_by_role('button', name='Cancel', exact=True)).to_be_disabled()
    expect(_row(page, ERROR_KEY).get_by_role('button', name='Delete', exact=True)).to_be_disabled()


def test_ttv_and_archive_sections_render_their_job_types(page, live_server):
    import time
    live_server.sql("""INSERT INTO jobs
      (key,type,instrument,obsdate,target,state,run_id,run_name,user_name,owner,elapsed,started_at)
      VALUES ('ttv_fit:TOI-1000/t1','ttv_fit','','','TOI-1000','done','t1','t1','admin','admin',0,?)""", (time.time(),))
    live_server.sql("""INSERT INTO jobs
      (key,type,instrument,obsdate,target,state,run_id,run_name,user_name,owner,elapsed,started_at)
      VALUES ('lco_archive_download:a1','lco_archive_download','muscat3','261001','TOI-1000','done','a1','archive download','admin','admin',0,?)""", (time.time(),))
    page.goto('/jobs')
    expect(page.locator('table[data-job-type="ttv_fit"] tr.job-row')).to_have_count(1)
    expect(page.locator('table[data-job-type="lco_archive_download"] tr.job-row')).to_have_count(1)
