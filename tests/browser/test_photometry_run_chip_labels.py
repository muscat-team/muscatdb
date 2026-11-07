"""Run chips must tell apart runs that share a run name.

A sinistro night reduced once per telescope (run ids ``tel06-default`` and
``tel08-default``) stores ``run_name: "default"`` for both, and the Run chips
showed only the run name, so the page read "Run: default default".
"""

from __future__ import annotations

import json
import shutil

import pytest
from playwright.sync_api import expect

from tests.browser.seed import RUN_ID

URL = '/photometry?inst=muscat3&date=261001&target=TOI-1000'
# One night reduced per telescope, and one reduced per site.
BY_TELESCOPE = ('telescope', {'tel06-default': '1m0-06', 'tel08-default': '1m0-08'})
BY_SITE = ('site', {'lsc-default': 'lsc', 'cpt-default': 'cpt'})
SAME_NAME_RUNS = BY_TELESCOPE[1]


def _add_same_name_runs(live_server, scope_key='telescope', runs=None):
    runs = runs or SAME_NAME_RUNS
    parent = live_server.root / 'prose/muscat3/261001/_runs/TOI-1000'
    for run_id, scope in runs.items():
        shutil.copytree(parent / RUN_ID, parent / run_id)
        (parent / run_id / '_webrun_meta.json').write_text(json.dumps(
            {'run_id': run_id, 'run_name': 'default', scope_key: scope}
        ))
    return parent


@pytest.mark.parametrize(("scope_key", "runs"), [BY_TELESCOPE, BY_SITE], ids=["telescope", "site"])
def test_runs_sharing_a_name_get_distinct_chips(page, live_server, scope_key, runs):
    parent = _add_same_name_runs(live_server, scope_key, runs)
    try:
        page.goto(URL)
        chips = page.locator('.site-chips:has(.site-label:text("Run:")) a.site-chip')
        labels = chips.all_inner_texts()
        assert len(labels) == len(set(labels)), f'duplicate run chip labels: {labels}'
        for run_id, scope in runs.items():
            chip = chips.filter(has_text=f'default · {scope}')
            expect(chip).to_have_count(1)
            chip.click()
            page.wait_for_url(f'**run={run_id}')
            expect(page.locator('a.site-chip.active', has_text=f'default · {scope}')).to_have_count(1)
    finally:
        page.wait_for_load_state('networkidle')
        page.context.close()
        for run_id in runs:
            shutil.rmtree(parent / run_id)


def test_a_unique_run_name_is_shown_unchanged(page, live_server):
    parent = _add_same_name_runs(live_server)
    try:
        page.goto(URL)
        expect(page.locator('a.site-chip', has_text=RUN_ID)).to_have_text(RUN_ID)
    finally:
        page.wait_for_load_state('networkidle')
        page.context.close()
        for run_id in SAME_NAME_RUNS:
            shutil.rmtree(parent / run_id)
