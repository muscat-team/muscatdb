import json
import pytest
from muscat_db import ephemeris_math, web


@pytest.mark.parametrize('ephem', [{}, {'t0': 2450000, 'period': 1}, {'t0': 2460000}, {'t0': None, 'period': 2}, {'t0': 2460000, 'period': -1}])
def test_missing_or_placeholder_reference_refused(ephem):
    response = web.api_ephemeris_calculate({'target': 'star', 'planets_ephem': {'b': ephem}})
    assert response.status_code == 400
    assert 'Planet b' in json.loads(response.body)['error']


def test_reduced_chi_squared_uses_timing_uncertainties():
    # Residuals (1, -2, 1) have zero mean and zero slope, so chi2 = 6.
    result = ephemeris_math.fit_linear_ephemeris([0, 1, 2], [101, 108, 121], [1, 1, 1], 100, 10, fit_method='weighted')
    assert result['reduced_chi2'] == pytest.approx(6)
    assert result['dof'] == 1 and result['n_fit'] == 3


def test_no_planets_is_not_a_missing_ephemeris():
    response = web.api_ephemeris_calculate({'target': 'star', 'planets_ephem': {}})
    assert response.status_code == 200
    assert json.loads(response.body) == {'ok': True, 'results': {}}


def test_supplied_period_order_reaches_csv_and_harmonic(monkeypatch):
    # V1298 Tau: P_c=8.25 < P_d=12.4 < P_b=24.14 < P_e=45 d, so the page sends "cdbe".
    ephem = {p: {'period': period, 't0': 2460000} for p, period in [('b',24.14), ('c',8.25), ('d',12.4), ('e',45)]}
    monkeypatch.setattr(web, '_transit_fit_available', lambda: lambda *a: True)
    selection = [{'kind':'manual', 'planet':p, 'epoch':0, 'tc':2460000+i, 'tc_unc':.001} for i,p in enumerate('bcde')]
    result, error = web._resolve_ttv_options({'planet_letters':'cdbe', 'planets_ephem':ephem, 'selection':selection})
    assert error is None
    assert result['planet_letters'] == 'cdbe'
    assert [line.split(',')[:3] for line in result['csv_content'].splitlines()[1:]] == [
        ['0','0','2460001.0'], ['1','0','2460002.0'], ['2','0','2460000.0'], ['3','0','2460003.0']]
