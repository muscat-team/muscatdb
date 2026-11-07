"""Uploading an external light curve from the transit-fit page (issue #382).

Driven as a user would: pick the files, assign a role per column, choose the
join key, save, follow the link to the new light curve. Nothing is stubbed;
the save writes into this module's own server tree.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect

DATA = Path(__file__).resolve().parents[1] / "data" / "lc_upload"
TARGET = "TOI-9999"
DATE = "260103"


def _role(page, which: str, column: str, role: str) -> None:
    page.select_option(f"table.lcu-map[data-which='{which}'] select[data-column='{column}']", label=role)


def _join(page, which: str, column: str) -> None:
    page.select_option(f"select.lcu-join[data-which='{which}']", column)


def test_upload_maps_columns_joins_saves_and_reaches_the_fit(page):
    page.goto("/transit-fit")
    page.click("#lc-upload > summary")
    save = page.locator("#lcu-save")
    msg = page.locator("#lcu-msg")
    expect(save).to_be_disabled()

    page.fill("#lcu-target", TARGET)
    page.fill("#lcu-date", DATE)
    page.fill("#lcu-facility", "ASTEP")
    page.select_option("#lcu-band", "r")

    with page.expect_response("**/api/lc-upload/inspect") as resp:
        page.set_input_files("#lcu-primary", str(DATA / "collab_lightcurve.csv"))
    assert resp.value.json()["ok"]
    expect(page.locator("table.lcu-map[data-which='primary'] tbody tr")).to_have_count(6)
    expect(msg).to_contain_text("Pick exactly one time column")
    # No second file yet: no join key to choose.
    expect(page.locator("select.lcu-join[data-which='primary']")).to_be_hidden()

    _role(page, "primary", "time_bjd", "time")
    _role(page, "primary", "rel_flux_T1", "flux")
    _role(page, "primary", "rel_flux_err_T1", "error")
    _role(page, "primary", "AIRMASS", "covariate")
    expect(save).to_be_enabled()

    with page.expect_response("**/api/lc-upload/inspect"):
        page.set_input_files("#lcu-ancillary", str(DATA / "collab_covariates.tsv"))
    expect(save).to_be_disabled()
    expect(msg).to_contain_text("Choose the column to join on in each file")
    # The time column is also the join key: a separate choice from its role.
    _join(page, "primary", "time_bjd")
    _join(page, "ancillary", "bjd")
    expect(msg).to_contain_text("Pick at least one covariate from the second file")
    _role(page, "ancillary", "bjd", "covariate")
    expect(msg).to_contain_text("cannot also be a covariate")
    _role(page, "ancillary", "bjd", "ignore")
    _role(page, "ancillary", "fwhm_arcsec", "covariate")
    expect(save).to_be_enabled()
    expect(msg).to_have_text("")

    with page.expect_response("**/api/lc-upload/save") as resp:
        save.click()
    result = resp.value.json()
    assert result["ok"], result
    expect(msg).to_contain_text(f"Saved {result['file']}")
    href = page.get_attribute("#lcu-msg a", "href")
    assert href == f"/transit-fit?inst=external&date={DATE}&target={TARGET}"

    # Save stays disabled after a save. Changing the mapping re-enables it, but
    # the same file is refused a second time, and the reason is shown.
    expect(save).to_be_disabled()
    _role(page, "primary", "AIRMASS", "ignore")
    expect(save).to_be_enabled()
    page.problems.allowed += ["HTTP 400: POST /api/lc-upload/save", "status of 400"]
    with page.expect_response("**/api/lc-upload/save") as again:
        save.click()
    assert again.value.status == 400
    expect(msg).to_contain_text("already uploaded")

    # The saved link opens the transit fit on the uploaded light curve.
    page.goto(href)
    expect(page.locator("#sel-inst")).to_have_value("external")
    expect(page.locator(".csv-card")).to_have_count(1)
    expect(page.locator(".csv-card .lc-name")).to_contain_text(result["file"])


def test_external_night_without_uploads_points_at_the_form(page):
    page.goto("/transit-fit?inst=external&date=260105&target=TOI-1")
    expect(page.locator(".empty-state h2")).to_have_text("No Uploaded Light Curves")
    expect(page.locator("#lc-upload")).to_have_attribute("open", "")
