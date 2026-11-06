"""Run chips must tell apart runs that share a run name.

A sinistro night reduced once per telescope (run ids ``tel06-default`` and
``tel08-default``) stores ``run_name: "default"`` for both, and the Run chips
showed only the run name, so the page read "Run: default default".
"""

from __future__ import annotations

import json
import shutil

from playwright.sync_api import expect

from tests.browser.seed import RUN_ID

URL = '/photometry?inst=muscat3&date=261001&target=TOI-1000'
SAME_NAME_RUNS = {'tel06-default': '1m0-06', 'tel08-default': '1m0-08'}


def _add_same_name_runs(live_server):
    parent = live_server.root / 'prose/muscat3/261001/_runs/TOI-1000'
    for run_id, telescope in SAME_NAME_RUNS.items():
        shutil.copytree(parent / RUN_ID, parent / run_id)
        (parent / run_id / '_webrun_meta.json').write_text(json.dumps(
            {'run_id': run_id, 'run_name': 'default', 'telescope': telescope}
        ))
    return parent


def test_runs_sharing_a_name_get_distinct_chips(page, live_server):
    parent = _add_same_name_runs(live_server)
    try:
        page.goto(URL)
        chips = page.locator('.site-chips:has(.site-label:text("Run:")) a.site-chip')
        labels = chips.all_inner_texts()
        assert len(labels) == len(set(labels)), f'duplicate run chip labels: {labels}'
        for run_id, telescope in SAME_NAME_RUNS.items():
            chip = chips.filter(has_text=telescope)
            expect(chip).to_have_count(1)
            expect(chip).to_contain_text('default')
            chip.click()
            page.wait_for_url(f'**run={run_id}')
            expect(page.locator('a.site-chip.active', has_text=telescope)).to_have_count(1)
    finally:
        page.wait_for_load_state('networkidle')
        page.context.close()
        for run_id in SAME_NAME_RUNS:
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
