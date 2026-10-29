"""Placeholder examples and styling remain distinct from entered values."""

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    "path,selector,placeholder,value",
    [
        ("/lco/schedule", "#sch-exp-g", "e.g. 30", "45"),
        ("/targets", 'input[data-col="filter"]', "e.g. g, r, i, z", "r"),
        ("/projects", "#create-project-desc", "Description (optional)…", "Follow-up"),
    ],
)
def test_placeholder_style_and_entry(page, theme, path, selector, placeholder, value):
    if path == "/lco/schedule":
        page.problems.allowed.extend(("HTTP 403: GET /api/lco/proposals", "status of 403"))
    page.goto(path)
    if path == "/lco/schedule":
        page.select_option("#sch-kind", "muscat")
    if theme == "dark":
        page.click("#theme-toggle")
    if path == "/projects":
        page.click("#create-project-btn")
    field = page.locator(selector)
    expect(field).to_have_value("")
    expect(field).to_have_attribute("placeholder", placeholder)
    styles = field.evaluate("""el => {
        const hint = getComputedStyle(el, '::placeholder');
        const probe = document.createElement('span');
        probe.style.color = 'var(--text-dim)';
        el.parentNode.appendChild(probe);
        const dim = getComputedStyle(probe).color;
        probe.remove();
        return {hint: hint.color, dim, text: getComputedStyle(el).color,
                font: hint.fontStyle, opacity: hint.opacity};
    }""")
    assert styles["hint"] == styles["dim"]
    assert styles["hint"] != styles["text"]
    assert styles["font"] == "italic"
    assert styles["opacity"] == "1"
    field.fill(value)
    expect(field).to_have_value(value)
    assert field.evaluate("el => getComputedStyle(el).color") == styles["text"]
    field.fill("")
    assert field.evaluate("el => el.matches(':placeholder-shown')")


def test_schedule_exposure_examples_are_not_values(page):
    page.problems.allowed.extend(("HTTP 403: GET /api/lco/proposals", "status of 403"))
    page.goto("/lco/schedule")
    for band in "griz":
        field = page.locator(f"#sch-exp-{band}")
        expect(field).to_have_attribute("placeholder", "e.g. 30")
        expect(field).to_have_value("")
    field = page.locator("#sch-effective-exptime")
    expect(field).to_have_attribute("placeholder", "e.g. 60")
    expect(field).to_have_value("")
