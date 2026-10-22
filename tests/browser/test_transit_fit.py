"""The /transit-fit page: results, light-curve selection, priors, options, run, delete.

``fit.start_fit`` / ``cancel_fit`` / ``delete_fit`` are stubbed in-process
(the harness refuses real launches); everything in front of them is real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.browser.helpers import reveal
from tests.browser.seed import FIT_RUN_ID

DATASET = "inst=muscat3&date=261001&target=TOI-1000"
URL = f"/transit-fit?{DATASET}"
OPT_KEY = "fit:options:muscat3:261001:TOI-1000"
CSVS = ["TOI-1000_muscat3_gp_261001.csv", "TOI-1000_muscat3_rp_261001.csv"]


@pytest.fixture
def fitter(monkeypatch):
    from muscat_db import transit_fit as fit

    calls = {"start": [], "cancel": [], "delete": []}

    def start_fit(inst, date, target, options, **kwargs):
        calls["start"].append({"options": options, **kwargs})
        return {"ok": True, "key": f"transit_fit:{inst}/{date}/{target}/{FIT_RUN_ID}", "run_id": FIT_RUN_ID}

    monkeypatch.setattr(fit, "start_fit", start_fit)
    monkeypatch.setattr(fit, "cancel_fit", lambda *a, **k: calls["cancel"].append(k) or {"ok": True})
    monkeypatch.setattr(fit, "delete_fit", lambda *a, **k: calls["delete"].append(k) or {"ok": True, "count": 1})
    return calls


def _csv_count(page) -> str:
    return page.inner_text("#csv-count")


def _query_toi_catalog(page) -> None:
    reveal(page, "#archive-source-select")
    page.select_option("#archive-source-select", "toi")
    with page.expect_response("**/api/transit-fit/query-archive**") as resp:
        page.click("#archive-query-btn")
    assert resp.value.json()["ok"]
    assert "Successfully loaded parameters for TOI-1000.01" in page.inner_text("#message-modal-text")
    page.click("#message-modal-ok")


def test_fit_results_are_shown(page):
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    plot = page.locator(f"img[src*='/run/{FIT_RUN_ID}/fit.png']").first
    page.wait_for_function("i => i.complete && i.naturalWidth > 0", arg=plot.element_handle())
    body = page.locator("body").inner_text()
    assert "t0[0]" in body and "2461314.86" in body
    assert page.locator("a", has_text="Download all output").count() == 1


def test_light_curve_selection_persists(page):
    page.goto(URL)
    assert _csv_count(page) == "2 / 2 selected"
    page.click("#deselect-all-csvs")
    assert _csv_count(page) == "0 / 2 selected"
    page.locator(".csv-checkbox").first.check()
    assert _csv_count(page) == "1 / 2 selected"

    page.reload()
    assert _csv_count(page) == "1 / 2 selected"
    page.click("#select-all-csvs")
    assert _csv_count(page) == "2 / 2 selected"


def test_archive_query_fills_the_priors(page):
    page.goto(URL)
    card = "planet-card-0"
    reveal(page, f"#planet-period-{card}")
    assert page.input_value(f"#planet-period-{card}") == ""
    _query_toi_catalog(page)
    page.wait_for_function(f"document.getElementById('planet-period-{card}').value !== ''")
    assert page.input_value(f"#planet-period-{card}").startswith("2.17")
    assert page.input_value(f"#planet-t0-{card}").startswith("2461314.86")
    assert page.input_value("#opt-teff").startswith("6100")


def test_options_persist_per_dataset(page):
    page.goto(URL)
    for control in ("#opt-tune", "#opt-draws", "#opt-spline"):
        reveal(page, control)
    page.fill("#opt-tune", "500")
    page.fill("#opt-draws", "750")
    page.locator("#opt-spline").check()
    page.goto("/jobs")
    page.goto(URL)
    assert page.input_value("#opt-tune") == "500"
    assert page.input_value("#opt-draws") == "750"
    assert page.is_checked("#opt-spline")
    # Saved under a per-dataset key, so other datasets keep their own options.
    assert page.evaluate(f"JSON.parse(localStorage.getItem('{OPT_KEY}')).tune") == "500"


def test_save_clear_and_restore_values(page):
    page.goto(URL)
    for control in ("#opt-tune", "#opt-teff", "#save-btn"):
        reveal(page, control)
    page.fill("#opt-teff", "5800")
    page.fill("#opt-tune", "1234")
    page.click("#save-btn")
    assert page.inner_text("#message-modal-title") == "Success"
    page.click("#message-modal-ok")

    page.click("#clear-btn")
    assert page.input_value("#opt-teff") == ""
    assert page.input_value("#opt-tune") == "2000"

    page.click("#restore-btn")
    assert page.input_value("#opt-teff") == "5800"
    assert page.input_value("#opt-tune") == "1234"


def test_add_planet_card(page):
    page.goto(URL)
    cards = page.locator("#planet-cards-container > *")
    assert cards.count() == 1
    reveal(page, "#add-planet-btn")
    page.click("#add-planet-btn")
    assert cards.count() == 2
    assert "c" in cards.nth(1).inner_text().lower()


def _complete_priors(page) -> None:
    """Fill what the TOI catalog lacks, as a user would before running."""
    card = "planet-card-0"
    for field, value in (
        ("#opt-teff_unc", "100"), ("#opt-logg", "4.3"), ("#opt-logg_unc", "0.1"),
        ("#opt-feh", "0.0"), ("#opt-feh_unc", "0.1"),
        (f"#planet-b-{card}", "0,1"), (f"#planet-ror-{card}", "0.065,0.01"),
    ):
        reveal(page, field)
        page.fill(field, value)


def test_test_run_sends_the_selected_curves_and_priors(page, fitter):
    page.goto(URL)
    _query_toi_catalog(page)
    _complete_priors(page)
    page.locator(".csv-checkbox").nth(1).uncheck()
    with page.expect_response("**/api/transit-fit/run") as resp:
        page.click("#run-test-btn")
    assert resp.value.ok
    call = fitter["start"][-1]
    assert call["test_run"] is True
    assert [Path(c).name for c in call["selected_csvs"]] == CSVS[:1]
    assert float(call["options"]["period"]) == pytest.approx(2.17)


def test_run_is_blocked_until_required_priors_are_set(page, fitter):
    page.goto(URL)
    page.click("#run-test-btn")
    assert page.locator(".field-error.visible").count() > 0
    assert fitter["start"] == []


@pytest.mark.xfail(
    strict=True,
    reason="an archive query with no b or Rp/R* overwrites their default uniform "
    "priors (0,1 and 0,0.5) with blanks, so the run then fails validation",
)
def test_archive_query_keeps_default_b_and_ror_priors(page):
    page.goto(URL)
    card = "planet-card-0"
    reveal(page, f"#planet-b-{card}")
    assert page.input_value(f"#planet-b-{card}") == "0,1"
    _query_toi_catalog(page)
    assert page.input_value(f"#planet-b-{card}") == "0,1"
    assert page.input_value(f"#planet-ror-{card}") == "0,0.5"


def test_delete_fit_after_confirmation(page, fitter):
    page.goto(URL)
    page.click("#delete-btn")
    with page.expect_response("**/api/transit-fit/delete"):
        page.click("#delete-confirm-btn")
    assert fitter["delete"][-1]["run_id"] == FIT_RUN_ID
