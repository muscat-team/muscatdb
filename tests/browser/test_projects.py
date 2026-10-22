"""Projects (tags): create, attach/detach targets, rename, describe, delete."""

from __future__ import annotations


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
