"""Projects (tags): create, attach/detach targets, rename, describe, delete."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect


def _create(page, name: str, description: str = "") -> None:
    page.goto("/projects")
    page.click("#create-project-btn")
    page.fill("#create-project-name", name)
    page.fill("#create-project-desc", description)
    with page.expect_navigation():
        page.click("#create-project-save")
    page.wait_for_url(f"**/tag?name={name}")


def _confirm(page) -> None:
    page.locator("#message-modal-ok").click()


def test_projects_list_links_to_seeded_project(page):
    page.goto("/projects")
    link = page.locator('a[href="/tag?name=demo"]')
    assert "Fixture project for the browser tests" in link.inner_text()
    link.click()
    assert page.locator("#project-targets-table tbody tr").get_attribute("data-norm-name") == "TOI1000"


def test_create_project_then_attach_and_detach_target(page, live_server):
    _create(page, "attach-test", "made in a browser test")
    assert "No targets attached yet" in page.locator("body").inner_text()

    page.fill("#attach-target-input", "TOI1000")
    with page.expect_navigation():
        page.click("#attach-target-btn")
    rows = page.locator("#project-targets-table tbody tr")
    assert rows.count() == 1 and rows.get_attribute("data-norm-name") == "TOI1000"
    assert live_server.sql("SELECT norm_name FROM target_tags WHERE tag = 'attach-test'") == [("TOI1000",)]

    page.click('.detach-btn[data-norm-name="TOI1000"]')
    with page.expect_navigation():
        _confirm(page)
    assert "No targets attached yet" in page.locator("body").inner_text()


def test_rename_project(page, live_server):
    _create(page, "rename-me")
    page.click("#project-name-cell")
    page.fill("#project-name-cell input", "renamed-project")
    with page.expect_navigation():
        page.click("#project-name-cell button.save")
    page.wait_for_url("**/tag?name=renamed-project")
    tags = {t for (t,) in live_server.sql("SELECT tag FROM tag_descriptions")}
    assert "renamed-project" in tags and "rename-me" not in tags


def test_description_renders_markdown_and_drops_html(page):
    _create(page, "describe-me")
    page.click("#project-desc-cell")
    page.fill(
        "#project-desc-cell textarea",
        "**Bold** and [a link](https://example.org)\n\n<img src=x onerror=alert(1)>",
    )
    with page.expect_navigation():
        page.click("#project-desc-cell button.save")
    cell = page.locator("#project-desc-cell")
    assert cell.locator("strong").inner_text() == "Bold"
    assert cell.locator('a[href="https://example.org"]').count() == 1
    assert cell.locator("img").count() == 0

    # Cancel restores the rendered description untouched.
    cell.click()
    cell.locator("textarea").fill("discarded")
    cell.locator("button", has_text="Cancel").click()
    assert cell.locator("strong").inner_text() == "Bold"


def test_delete_project(page, live_server):
    _create(page, "delete-me")
    page.click("#delete-project-btn")
    with page.expect_navigation():
        _confirm(page)
    page.wait_for_url("**/projects")
    assert live_server.sql("SELECT tag FROM tag_descriptions WHERE tag = 'delete-me'") == []
    assert page.locator('a[href="/tag?name=delete-me"]').count() == 0


def test_empty_project_name_is_blocked_and_escape_cancels(page, live_server):
    page.goto('/projects')
    before = live_server.sql('SELECT tag FROM tag_descriptions ORDER BY tag')
    page.click('#create-project-btn')
    page.fill('#create-project-name', '   ')
    page.click('#create-project-save')
    expect(page.locator('#create-project-name')).to_be_focused()
    expect(page.locator('#create-project-overlay')).to_be_visible()
    page.locator('#create-project-name').press('Escape')
    expect(page.locator('#create-project-overlay')).not_to_be_visible()
    assert live_server.sql('SELECT tag FROM tag_descriptions ORDER BY tag') == before


def test_failed_create_shows_the_reason_and_allows_retry(page):
    page.route('**/api/tags', lambda r: r.fulfill(status=503, json={'detail': 'database busy'}))
    page.problems.allowed += ['HTTP 503: POST /api/tags', 'status of 503']
    page.goto('/projects')
    page.click('#create-project-btn')
    page.fill('#create-project-name', 'retry-create')
    page.click('#create-project-save')
    expect(page.locator('#message-modal-text')).to_contain_text('database busy')
    page.click('#message-modal-ok')
    page.unroute('**/api/tags')
    _create(page, 'retry-create')


def test_cancel_rename_and_delete_preserve_project(page, live_server):
    _create(page, 'keep-project')
    page.click('#project-name-cell')
    page.fill('#project-name-cell input', 'discarded')
    page.locator('#project-name-cell input').press('Escape')
    expect(page.locator('#project-name-cell')).to_contain_text('keep-project')
    page.click('#delete-project-btn')
    page.click('#message-modal-cancel')
    assert live_server.sql("SELECT tag FROM tag_descriptions WHERE tag = 'keep-project'") == [('keep-project',)]


def test_project_table_sorts_targets_and_exports_csv(page):
    _create(page, 'sort-export')
    for target in ('TOI1000', 'HATP1'):
        page.fill('#attach-target-input', target)
        with page.expect_navigation():
            page.click('#attach-target-btn')
    header = page.locator('#project-targets-table th[data-sort-attr="normName"]')
    header.click()
    names = page.locator('#project-targets-table tbody tr').evaluate_all('rs => rs.map(r => r.dataset.normName)')
    assert len(names) == 2
    header.click()
    assert page.locator('#project-targets-table tbody tr').evaluate_all('rs => rs.map(r => r.dataset.normName)') == list(reversed(names))
    with page.expect_download() as download:
        page.locator('a.csv-btn').click()
    contents = Path(download.value.path()).read_text()
    assert 'TOI1000' in contents and 'HATP1' in contents


def test_duplicate_project_and_rename_conflicts_leave_existing_data_intact(page, live_server):
    page.problems.allowed += ['HTTP 409', 'status of 409']
    page.goto('/projects')
    page.click('#create-project-btn')
    page.fill('#create-project-name', 'demo')
    page.click('#create-project-save')
    expect(page.locator('#message-modal-text')).to_contain_text('already exists')
    page.click('#message-modal-ok')
    _create(page, 'conflict-project', 'keep this description')
    page.click('#project-name-cell')
    page.fill('#project-name-cell input', 'demo')
    page.locator('#project-name-cell input').press('Enter')
    expect(page.locator('#message-modal-text')).to_contain_text('already exists')
    page.click('#message-modal-ok')
    expect(page.locator('#project-name-cell')).to_contain_text('conflict-project')
    assert live_server.sql("SELECT description FROM tag_descriptions WHERE tag='conflict-project'") == [('keep this description',)]


def test_unknown_target_attachment_is_reported_without_adding_a_row(page):
    page.problems.allowed += ['HTTP 404: PUT /api/targets/', 'status of 404']
    _create(page, 'unknown-target')
    page.fill('#attach-target-input', 'does-not-exist')
    page.click('#attach-target-btn')
    expect(page.locator('#message-modal-text')).to_contain_text('Could not attach target')
    page.click('#message-modal-ok')
    expect(page.get_by_role('heading', name='No targets attached yet')).to_be_visible()
