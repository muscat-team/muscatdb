"""Transit-fit Run chips must tell apart fit runs that share a run name.

Fits of a sinistro night made once per telescope store ``run_name: default``
for both runs, and the Run chips showed only the run name ("default default"),
the same bug the photometry page had.
"""

from __future__ import annotations

import shutil

import yaml
import pytest
from playwright.sync_api import expect

from tests.browser.seed import FIT_RUN_ID

URL = '/transit-fit?inst=muscat3&date=261001&target=TOI-1000'
# One night reduced per telescope, and one reduced per site.
BY_TELESCOPE = ('telescope', {'tel06-default': '1m0-06', 'tel08-default': '1m0-08'})
BY_SITE = ('site', {'lsc-default': 'lsc', 'cpt-default': 'cpt'})
SAME_NAME_RUNS = BY_TELESCOPE[1]


def _add_same_name_fit_runs(live_server, scope_key='telescope', runs=None):
    runs = runs or SAME_NAME_RUNS
    parent = live_server.root / 'timer/muscat3/261001/TOI-1000'
    for run_id, scope in runs.items():
        shutil.copytree(parent / FIT_RUN_ID, parent / run_id)
        (parent / run_id / 'meta.yaml').write_text(yaml.safe_dump(
            {'run_id': run_id, 'run_name': 'default', 'mode': 'full', scope_key: scope}
        ))
    return parent


@pytest.mark.parametrize(("scope_key", "runs"), [BY_TELESCOPE, BY_SITE], ids=["telescope", "site"])
def test_fit_runs_sharing_a_name_get_distinct_chips(page, live_server, scope_key, runs):
    parent = _add_same_name_fit_runs(live_server, scope_key, runs)
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