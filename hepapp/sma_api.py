"""SMA Monitoring API client (developer.sma.de).

Officijelni API umjesto scrapinga Sunny Portala. Daje EnergyBalance s
5-min rezolucijom za današnji dan + povijest. Auth: OAuth2 (Authorization
Code Grant + refresh_token).

NAPOMENA: SMA ne nudi self-service registraciju. Client credentialse moraš
zatražiti direktno od SMA — pošalji kontakt formu s logo/ToS/privacy/redirect
URL-ovima na https://developer.sma.de/contact. Vidi SMA_API_SETUP.md za korake.

Konfiguracija (env vars):
    SMA_API_BASE_URL       — default https://monitoring.smaapis.de
                             (sandbox: https://sandbox.smaapis.de/monitoring)
    SMA_API_TOKEN_URL      — default https://auth.smaapis.de/oauth2/token
                             (sandbox: https://sandbox-auth.smaapis.de/oauth2/token)
    SMA_API_CLIENT_ID      — od SMA (manualno via kontakt forma)
    SMA_API_CLIENT_SECRET  — od SMA
    SMA_API_REFRESH_TOKEN  — od plant-owner autorizacije (Code Grant Step 2)
    SMA_API_PLANT_ID       — ID elektrane (iz GET /v1/plants)

Endpointi koje koristimo:
    GET /v1/plants
    GET /v1/plants/{plantId}/measurements/sets/EnergyBalance/Recent
    GET /v1/plants/{plantId}/measurements/sets/EnergyBalance/Day?Date=YYYY-MM-DD
    GET /v1/plants/{plantId}/measurements/sets/EnergyBalance/Month?Date=YYYY-MM
"""

import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


class SmaApiClient:
    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        base_url: Optional[str] = None,
        oauth_url: Optional[str] = None,
    ):
        self.base_url      = (base_url or os.environ.get('SMA_API_BASE_URL', 'https://monitoring.smaapis.de')).rstrip('/')
        self.oauth_url     = oauth_url or os.environ.get('SMA_API_TOKEN_URL', 'https://auth.smaapis.de/oauth2/token')
        self.client_id     = client_id or os.environ.get('SMA_API_CLIENT_ID', '')
        self.client_secret = client_secret or os.environ.get('SMA_API_CLIENT_SECRET', '')
        self.refresh_token = refresh_token or os.environ.get('SMA_API_REFRESH_TOKEN', '')
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        # retry samo na GET (token POST se ne retry-a — rotacija refresh_tokena)
        from netutil import retry_session
        self.s = retry_session()
        self.s.headers.update({'Accept': 'application/json'})

    # ---- AUTH ----

    def _refresh_access_token(self):
        """Iskoristi refresh_token za novi access_token."""
        if not self.refresh_token:
            raise RuntimeError(
                'SMA_API_REFRESH_TOKEN nije postavljen. '
                'Vidi docs/SMA_API_SETUP.md.'
            )
        data = {
            'grant_type':    'refresh_token',
            'refresh_token': self.refresh_token,
        }
        auth = None
        if self.client_id and self.client_secret:
            auth = (self.client_id, self.client_secret)
        r = requests.post(self.oauth_url, data=data, auth=auth, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f'SMA OAuth refresh fail {r.status_code}: {r.text[:200]}')
        j = r.json()
        self._access_token = j['access_token']
        self._token_expires_at = time.time() + int(j.get('expires_in', 3600)) - 60
        # ako server vraća rotirani refresh_token, ažuriraj ga
        if j.get('refresh_token'):
            self.refresh_token = j['refresh_token']
            log.info('SMA: refresh_token rotiran — ažuriraj .env')
        log.info('SMA: novi access_token (expires_in=%ss)', j.get('expires_in'))

    def _ensure_token(self):
        if not self._access_token or time.time() >= self._token_expires_at:
            self._refresh_access_token()

    # ---- HTTP ----

    def _get(self, path: str, params: dict = None) -> dict:
        self._ensure_token()
        url = f'{self.base_url}{path}'
        headers = {'Authorization': f'Bearer {self._access_token}'}
        r = self.s.get(url, headers=headers, params=params or {}, timeout=30)
        if r.status_code == 401:
            # token istekao mid-flight — pokušaj jednom refresh
            log.info('SMA 401 — refreshing token')
            self._refresh_access_token()
            headers = {'Authorization': f'Bearer {self._access_token}'}
            r = self.s.get(url, headers=headers, params=params or {}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f'SMA API GET {path} fail {r.status_code}: {r.text[:200]}')
        return r.json()

    # ---- PUBLIC METODE ----

    def plants(self) -> list:
        return self._get('/v1/plants').get('plants', [])

    def energy_balance_recent(self, plant_id: str) -> dict:
        return self._get(f'/v1/plants/{plant_id}/measurements/sets/EnergyBalance/Recent')

    def energy_balance_day(self, plant_id: str, datum: str) -> dict:
        """Date format: YYYY-MM-DD. Vraća 5-min resolution za jedan dan."""
        return self._get(
            f'/v1/plants/{plant_id}/measurements/sets/EnergyBalance/Day',
            params={'Date': datum, 'WithTotal': 'true'},
        )

    def energy_balance_month(self, plant_id: str, mjesec: str) -> dict:
        """Date format: YYYY-MM. Vraća dnevne sume."""
        return self._get(
            f'/v1/plants/{plant_id}/measurements/sets/EnergyBalance/Month',
            params={'Date': mjesec, 'WithTotal': 'true'},
        )
