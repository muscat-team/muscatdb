"""Exercise date extension in the real helper, with only harmonic stubbed."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def helper(monkeypatch):
    modules = {name: types.ModuleType(name) for name in
               ("harmonic", "harmonic.harmonic", "harmonic.model")}
    modules["harmonic.harmonic"].Harmonic = object
    modules["harmonic.model"].model = lambda params, planets, epochs, *a, **k: (
        params["t0_b"] + params["per_b"] * epochs
    )
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[1] / "src/muscat_db/_ttv_model_helper.py"
    spec = importlib.util.spec_from_file_location("ttv_model_undertest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("offset", [0, 2454833])
@pytest.mark.parametrize("days, last_epoch", [(30, 7), (-5, 2), (None, 2)])
def test_date_extension_uses_fit_time_system(helper, monkeypatch, tmp_path, offset, days, last_epoch):
    t0_bjd = 2455833.0
    t0 = t0_bjd - offset
    (tmp_path / "fit_config.json").write_text(json.dumps({"letters": "b", "t_offset": offset}))
    fake = types.SimpleNamespace(
        planet_letters=["b"], non_transiting_outer=False, phase_offsets=False,
        spec=types.SimpleNamespace(t_ref=0), _require_chain=lambda: None,
        times=pd.DataFrame({"planet": ["b", "b"], "epoch": [0, 2],
                            "tc": [t0, t0 + 10], "tc_unc": [0.001, 0.001]}),
        flatchain=pd.DataFrame({"t0_b": [t0], "per_b": [5.0]}),
    )
    monkeypatch.setattr(helper, "Harmonic", lambda **kw: fake)
    result = helper.evaluate(tmp_path, None if days is None else t0_bjd + days)
    assert result["t_offset"] == offset
    assert [p["epoch"] for p in result["points"]["b"]] == list(range(last_epoch + 1))
    np.testing.assert_allclose([p["tc"] for p in result["points"]["b"]],
                               t0 + 5 * np.arange(last_epoch + 1), rtol=0, atol=1e-8)
