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
