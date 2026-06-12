"""Zajednicki HTTP helperi za scrapere i web app.

- retry_session(): requests.Session s eksponencijalnim backoffom na
  prolazne greske (429/5xx + connection errori).
- ha_verify(): SSL verifikacija prema Home Assistantu. Default ukljucena;
  HA_VERIFY_SSL=0 za self-signed certove (gasi i urllib3 warninge).
"""

import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def retry_session(total=3, backoff=1.5, allow_post=False, session=None):
    """Vrati requests.Session s retry adapterom.

    allow_post=True retry-a i POST (koristi samo za idempotentne POST-ove,
    npr. HEP read-preko-POST API ili HA set_state).
    """
    s = session or requests.Session()
    retry = Retry(
        total=total,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None if allow_post else Retry.DEFAULT_ALLOWED_METHODS,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount('https://', adapter)
    s.mount('http://', adapter)
    return s


def ha_verify() -> bool:
    """SSL verify za HA pozive. HA_VERIFY_SSL=0/false/no je iskljucuje."""
    verify = os.environ.get('HA_VERIFY_SSL', '1').strip().lower() not in ('0', 'false', 'no')
    if not verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return verify
