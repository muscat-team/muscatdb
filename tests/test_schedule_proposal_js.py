"""The schedule page's plan and request-window logic, run with node against the template.

Each transit offers booking plans (single site, either site, relay); its plan's
request windows start as the server's spans and can be dragged/resized on the
visibility plot, clamped to the observable run. These functions are pulled
verbatim out of lco_schedule.html so the test exercises the shipped code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "muscat_db" / "templates" / "lco_schedule.html"
node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node not installed")

_FUNCTIONS = (
    "utcMs", "isoZ", "runAround", "clampProposal", "midUncMinutes",
    "isSingleFilterKind", "isMuscatKind", "kindAvailableAt", "kindsAtSite", "siteSchedulable", "planSites",
    "planSchedulable", "planLabel", "windowPlans", "eitherForInstrument", "defaultPlanId", "planLegs",
)
_VARS = (
    "PROPOSAL_SNAP_MS", "PROPOSAL_MIN_MS", "WINDOW_PLAN", "SINGLE_FILTER_KINDS",
    "SINGLE_FILTER_SITES", "KIND_INSTRUMENT_TYPE", "_2M_SITES", "KIND_LABEL",
)


def _function_source(html: str, name: str) -> str:
    start = html.index(f"function {name}(")
    depth = 0
    for i in range(html.index("{", start), len(html)):
        depth += {"{": 1, "}": -1}.get(html[i], 0)
        if depth == 0:
            return html[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _var_source(html: str, name: str) -> str:
    start = html.index(f"var {name} = ")
    end = html.index(";", start)
    brace = html.find("{", start, end)
    if brace == -1:
        return html[start:end + 1]
    depth = 0
    for i in range(brace, len(html)):
        depth += {"{": 1, "}": -1}.get(html[i], 0)
        if depth == 0:
            return html[start:html.index(";", i) + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _run(expr: str, policy: str = "relay", by_site: dict | None = None, kind: str = "sinistro"):
    html = TEMPLATE.read_text()
    variables = [_var_source(html, v) for v in _VARS]
    funcs = [_function_source(html, f) for f in _FUNCTIONS]
    # Page globals the plan helpers read: the Default plan dropdown and LCO's
    # live per-site instrument list (null = unknown, everything schedulable).
    stubs = [
        f"const val = (id) => ({{'sch-plan-policy': {json.dumps(policy)}, 'sch-kind': {json.dumps(kind)}}})[id] || '';",
        f"var siteAvailability = {json.dumps({'by_site': by_site} if by_site else None)};",
        "var SITE_OFFLINE_SUFFIX = ' — currently unavailable';",
    ]
    script = "\n".join(stubs + variables + funcs) + textwrap.dedent(
        f"""
        const M = (s) => utcMs('2026-10-21T' + s + ':00Z');
        const H = (ms) => isoZ(ms).slice(11, 16);
        const fmt = (p) => p && [H(p.start), H(p.end)];
        console.log(JSON.stringify({expr}));
        """
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


RUN = "{start: M('07:00') + 20000, end: M('12:05') - 20000}"


def test_drag_keeps_span_and_stops_at_run_edge():
    assert _run(f"fmt(clampProposal({RUN}, M('09:00'), M('13:00'), true))") == ["08:04", "12:04"]
    assert _run(f"fmt(clampProposal({RUN}, M('05:00'), M('06:00'), true))") == ["07:01", "08:01"]


def test_resize_clamps_each_edge_and_keeps_minimum_span():
    assert _run(f"fmt(clampProposal({RUN}, M('06:00'), M('13:00'), false))") == ["07:01", "12:04"]
    # Start dragged past the end: the window collapses to the 10 min minimum.
    assert _run(f"fmt(clampProposal({RUN}, M('12:30'), M('09:00'), false))") == ["11:54", "12:04"]


def test_run_around_picks_the_run_overlapping_the_window_most():
    data = (
        "{observable_runs: ["
        "{start: '2026-10-21T01:00:00.000', end: '2026-10-21T02:00:00.000'},"
        "{start: '2026-10-21T07:00:00.000', end: '2026-10-21T12:05:00.000'}]}"
    )
    assert _run(f"fmt(runAround({data}, M('06:25'), M('10:55')))") == ["07:00", "12:05"]
    assert _run(f"runAround({data}, M('03:00'), M('04:00'))") is None


# The 2026-10-26 transit: TFN full, CPT partial (ingress), either, and a relay.
PLANS = json.dumps([
    {"type": "single", "id": "site:tfn", "site": "tfn", "coverage": "full", "ingress": True, "egress": True,
     "start": "2026-10-26T01:12:00Z", "end": "2026-10-26T05:28:00Z"},
    {"type": "single", "id": "site:cpt", "site": "cpt", "coverage": "partial", "ingress": True, "egress": False,
     "start": "2026-10-26T01:12:00Z", "end": "2026-10-26T02:45:00Z"},
    {"type": "either", "id": "either", "sites": ["tfn", "cpt"],
     "start": "2026-10-26T01:12:00Z", "end": "2026-10-26T05:28:00Z"},
    {"type": "relay", "id": "relay:cpt>tfn", "sites": ["cpt", "tfn"], "overlap_min": 30.0, "gap_min": 0.0,
     "legs": [{"site": "cpt", "start": "2026-10-26T01:12:00Z", "end": "2026-10-26T02:14:00Z"},
              {"site": "tfn", "start": "2026-10-26T01:44:00Z", "end": "2026-10-26T05:28:00Z"}]},
])
W = f"{{observability: {{plans: {PLANS}}}, start: '2026-10-26T00:27:00Z', end: '2026-10-26T06:13:00Z'}}"
TFN_OFFLINE = {"tfn": [], "cpt": ["0M4-SCICAM-QHY600", "1M0-SCICAM-SINISTRO"]}


def test_default_plan_prefers_a_full_site():
    assert _run(f"defaultPlanId({W})") == "site:tfn"
    assert _run(f"defaultPlanId({W})", policy="single") == "site:tfn"
    assert _run(f"defaultPlanId({W})", policy="either") == "either"


def test_default_plan_avoids_a_site_lco_lists_as_offline():
    """TFN has no schedulable instrument right now: the full-site and relay
    plans need it, so the default falls to CPT (or Either, which LCO can still
    run at CPT), and TFN's plans are labelled offline."""
    assert _run(f"defaultPlanId({W})", by_site=TFN_OFFLINE) == "site:cpt"
    assert _run(f"defaultPlanId({W})", policy="either", by_site=TFN_OFFLINE) == "either"
    labels = _run(f"windowPlans({W}).map(planLabel)", by_site=TFN_OFFLINE)
    assert labels == [
        "Full @ TFN · offline", "Partial @ CPT (ingress)", "Either TFN / CPT (Sinistro)",
        "Relay CPT → TFN (30 min overlap) · offline",
    ]


def test_plan_legs_follow_the_plan_unless_adjusted_on_the_plot():
    relay = f"windowPlans({W})[3]"
    assert _run(f"planLegs({W}, {relay}).map(l => [l.site, l.start.slice(11, 16), l.end.slice(11, 16)])") == [
        ["cpt", "01:12", "02:14"], ["tfn", "01:44", "05:28"]]
    assert _run(f"planLegs({W}, windowPlans({W})[2])[0].sites") == ["tfn", "cpt"]
    adjusted = (f"Object.assign({W}, {{boxes: {{'relay:cpt>tfn': [{{site: 'cpt', start: 'a', end: 'b'}}, "
                f"{{site: 'tfn', start: 'c', end: 'd'}}]}}}})")
    assert _run(f"planLegs({adjusted}, {relay}).map(l => l.start)") == ["a", "c"]
    # No plans (no coordinates): the padded window with no site.
    assert _run("planLegs({start: 's', end: 'e'}, WINDOW_PLAN)") == [{"site": "", "start": "s", "end": "e"}]


def test_mid_uncertainty_propagates_from_t0_not_from_the_first_window():
    """Mid unc grows with the number of periods since t0 (epoch_abs), not
    with the table's display epoch, which restarts at 0 in every range.
    TIC434398831 b (NASA): t0 +- 0.003 d, P +- 1.15e-5 d, ~778 epochs on."""
    w = "{epoch: 0, epoch_abs: 778}"
    got = _run(f"midUncMinutes({w}, 0.003, 1.15e-5)")
    assert got == pytest.approx(1440 * (0.003 ** 2 + (778 * 1.15e-5) ** 2) ** 0.5, rel=1e-9)
    assert 13.5 < got < 13.7
    # No uncertainty on either input: nothing to propagate.
    assert _run(f"midUncMinutes({w}, null, null)") is None


def _either_window(sites):
    singles = [
        {"type": "single", "id": f"site:{s}", "site": s, "coverage": "partial", "ingress": i == 0, "egress": i > 0,
         "start": f"2026-10-22T{8 + i:02d}:00:00Z", "end": f"2026-10-22T{11 + i:02d}:00:00Z"}
        for i, s in enumerate(sites)
    ]
    either = {"type": "either", "id": "either", "sites": sites,
              "start": "2026-10-22T08:00:00Z", "end": f"2026-10-22T1{len(sites)}:00:00Z"}
    return f"{{observability: {{plans: {json.dumps(singles + [either])}}}}}"


def test_either_needs_the_selected_instrument_at_every_site():
    """One either-site request has one instrument: ELP (1 m) / OGG (2 m) share
    neither Sinistro nor MuSCAT, so no Either is offered for them -- a relay
    (one request per site) remains the way to combine them."""
    w = _either_window(["elp", "ogg"])
    ids = lambda kind: _run(f"windowPlans({w}).map(p => p.id)", kind=kind)  # noqa: E731
    assert "either" not in " ".join(ids("sinistro"))
    assert "either" not in " ".join(ids("muscat"))
    # QHY600 is at every site, so it can be offered as an either-site request.
    assert ids("qhy600")[-1] == "either"


def test_either_keeps_only_sites_hosting_the_instrument():
    """OGG / COJ / ELP under Sinistro: OGG has no 1 m, so Either covers COJ and
    ELP only, spanning just their windows."""
    w = _either_window(["ogg", "coj", "elp"])
    either = _run(f"windowPlans({w}).filter(p => p.type === 'either')[0]")
    assert either["sites"] == ["coj", "elp"]
    assert either["id"] == "either:coj,elp"
    assert (either["start"], either["end"]) == ("2026-10-22T09:00:00Z", "2026-10-22T13:00:00Z")
    assert _run(f"planLabel(windowPlans({w}).filter(p => p.type === 'either')[0])") == "Either COJ / ELP (Sinistro)"
    # MuSCAT is at OGG and COJ only.
    assert _run(f"windowPlans({w}).filter(p => p.type === 'either')[0].sites", kind="muscat") == ["ogg", "coj"]
