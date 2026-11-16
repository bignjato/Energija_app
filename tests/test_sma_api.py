"""Testovi SMA Monitoring API klijenta (token refresh + 401 retry)."""

import pytest

from hepapp.sma_api import SmaApiClient


class FakeResp:
    def __init__(self, status, payload=None, text=''):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _client(**kw):
    return SmaApiClient(client_id='cid', client_secret='sec',
                        refresh_token='rtok', **kw)


def test_refresh_token_postavlja_access_token(monkeypatch):
    c = _client()
    monkeypatch.setattr('hepapp.sma_api.requests.post',
                        lambda *a, **k: FakeResp(200, {'access_token': 'AT', 'expires_in': 3600}))
    c._ensure_token()
    assert c._access_token == 'AT'
    assert c._token_expires_at > 0


def test_refresh_token_rotacija(monkeypatch):
    c = _client()
    monkeypatch.setattr('hepapp.sma_api.requests.post',
                        lambda *a, **k: FakeResp(200, {'access_token': 'AT',
                                                       'refresh_token': 'NOVI', 'expires_in': 3600}))
    c._refresh_access_token()
    assert c.refresh_token == 'NOVI'


def test_refresh_bez_tokena_baca():
    c = SmaApiClient(client_id='c', client_secret='s', refresh_token='')
    with pytest.raises(RuntimeError):
        c._refresh_access_token()


def test_refresh_fail_baca(monkeypatch):
    c = _client()
    monkeypatch.setattr('hepapp.sma_api.requests.post',
                        lambda *a, **k: FakeResp(400, text='bad'))
    with pytest.raises(RuntimeError):
        c._refresh_access_token()


def test_get_401_pa_refresh_retry(monkeypatch):
    c = _client()
    c._access_token = 'STARI'
    c._token_expires_at = 9e18  # token "vrijedi" pa _ensure_token ne osvježava

    calls = {'get': 0, 'refresh': 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls['get'] += 1
        if calls['get'] == 1:
            return FakeResp(401, text='expired')
        return FakeResp(200, {'ok': True})

    def fake_refresh():
        calls['refresh'] += 1
        c._access_token = 'NOVI'

    monkeypatch.setattr(c.s, 'get', fake_get)
    monkeypatch.setattr(c, '_refresh_access_token', fake_refresh)

    out = c._get('/v1/plants')
    assert out == {'ok': True}
    assert calls['get'] == 2
    assert calls['refresh'] == 1


def test_get_trajni_fail_baca(monkeypatch):
    c = _client()
    c._access_token = 'AT'
    c._token_expires_at = 9e18
    monkeypatch.setattr(c.s, 'get',
                        lambda *a, **k: FakeResp(500, text='server error'))
    with pytest.raises(RuntimeError):
        c._get('/v1/plants')


# ---- sma_live_ts: format sma_live.ts (UTC s eksplicitnim offsetom) ----

def test_sma_live_ts_format_i_konverzija():
    import re
    from zoneinfo import ZoneInfo
    from hepapp.sma_api import sma_live_ts

    zg = ZoneInfo('Europe/Zagreb')
    assert sma_live_ts('2026-11-15T22:48:35Z') == '2026-11-15T22:48:35+00:00'
    assert sma_live_ts('2026-07-10T12:00:00.123+02:00') == '2026-07-10T10:00:00+00:00'
    # naivno: default UTC; uz naive_tz lokalno -> UTC (ljeto +2, zima +1)
    assert sma_live_ts('2026-07-10T12:00:00') == '2026-07-10T12:00:00+00:00'
    assert sma_live_ts('2026-07-10T12:00:00', naive_tz=zg) == '2026-07-10T10:00:00+00:00'
    assert sma_live_ts('2026-01-10T12:00:00', naive_tz=zg) == '2026-01-10T11:00:00+00:00'
    # bez vrijednosti / smeće -> sada, u istom formatu
    for v in (None, '', 'nije-datum'):
        assert re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00', sma_live_ts(v))


# ---- sma_15min_ts + sma_api_scraper._save_day: sma_15min.ts je UTC sa 'Z' ----

def test_sma_15min_ts_format_i_konverzija():
    from zoneinfo import ZoneInfo
    from hepapp.sma_api import sma_15min_ts

    zg = ZoneInfo('Europe/Zagreb')
    assert sma_15min_ts('2026-07-15T12:00:00', naive_tz=zg) == '2026-07-15T10:00:00Z'
    assert sma_15min_ts('2026-01-15T12:00:00', naive_tz=zg) == '2026-01-15T11:00:00Z'
    assert sma_15min_ts('2026-07-15T12:00:00+02:00', naive_tz=zg) == '2026-07-15T10:00:00Z'
    assert sma_15min_ts('2026-07-15T10:00:00Z') == '2026-07-15T10:00:00Z'
    # povijesni uzorak bez valjanog vremena se preskače (nema "sada" fallbacka)
    for v in (None, '', 'nije-datum'):
        assert sma_15min_ts(v) is None


def test_save_day_sprema_utc_z(tmp_path):
    import sqlite3
    import sma_api_scraper as sas

    class Api:
        def energy_balance_day(self, plant_id, datum):
            return {'resolution': 'FiveMinutes', 'set': [
                {'time': '2026-07-15T00:05:00', 'pvGeneration': 0},      # lokalno 00:05 = 14.7. 22:05Z
                {'time': '2026-07-15T12:00:00', 'pvGeneration': 3000},
                {'time': None, 'pvGeneration': 1},
                {'time': 'smeće', 'pvGeneration': 1},
            ]}

    conn = sqlite3.connect(tmp_path / 'api.db')
    sas._ensure_schema(conn)
    assert sas._save_day(conn, 'P1', '2026-07-15', Api()) == 2
    assert sas._save_day(conn, 'P1', '2026-07-15', Api()) == 2   # upsert, bez duplikata
    rows = conn.execute('SELECT ts, pv_w_total FROM sma_15min ORDER BY ts').fetchall()
    assert rows == [('2026-07-14T22:05:00Z', 0), ('2026-07-15T10:00:00Z', 3000)]
    conn.close()
