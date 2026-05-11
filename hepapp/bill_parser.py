"""HEP račun PDF parser.

Pokušava izvući standardna polja iz HEP mjesečnog računa. Robusan na varijacije
formata — vraća što je našao i tekst dump za debugging.

Polja:
    period       : "MM/YYYY"
    od / do      : "YYYY-MM-DD"
    iznos        : float (€)
    kwh_vt       : float
    kwh_nt       : float
    kwh_plus     : float (vt+nt)
    kwh_minus    : float (predaja)
    opskrba      : float
    mreza        : float
    pdv          : float
"""

import io
import re
from datetime import datetime


def _to_float(s: str):
    """Hrvatski format → float. '254,72' → 254.72, '1.126,50' → 1126.50."""
    if s is None:
        return None
    s = s.strip().replace('€', '').replace('EUR', '').strip()
    # ako ima i točku i zarez: točka je tisućice
    if '.' in s and ',' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def _search(patterns, text, group=1):
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return m.group(group)
    return None


def extract_text(pdf_bytes: bytes) -> str:
    """PDF → tekst. Fallback ako pdfplumber nije dostupan."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return '\n'.join((page.extract_text() or '') for page in pdf.pages)
    except ImportError:
        raise RuntimeError('pdfplumber nije instaliran u image-u')


def parse_hep_bill(text: str) -> dict:
    """Heuristički regex parser za HEP račun."""
    res = {
        'period': None, 'od': None, 'do': None,
        'iznos': None,
        'kwh_vt': None, 'kwh_nt': None,
        'kwh_plus': None, 'kwh_minus': None,
        'opskrba': None, 'mreza': None, 'pdv': None,
        'raw_excerpt': text[:1500],
    }

    # Razdoblje: "od 01.05.2026 do 31.05.2026" ili "razdoblje 01.05.-31.05.2026"
    m = re.search(
        r'(?:razdoblje|obra[čc]un[a-zšđž]*|od)\s*(\d{1,2})\.(\d{1,2})\.(\d{4})[^\d]+(\d{1,2})\.(\d{1,2})\.(\d{4})',
        text, flags=re.IGNORECASE,
    )
    if m:
        try:
            res['od'] = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            res['do'] = f"{m.group(6)}-{int(m.group(5)):02d}-{int(m.group(4)):02d}"
            res['period'] = f"{int(m.group(2)):02d}/{m.group(3)}"
        except Exception:
            pass

    # Iznos za uplatu / Ukupno za platiti
    iznos_str = _search([
        r'(?:iznos\s*za\s*uplatu|ukupno\s*za\s*platiti|za\s*uplatu)[^\d]{0,30}([\d\.,]+)\s*(?:€|EUR)',
        r'(?:€|EUR)\s*([\d\.,]+)\s*\n?\s*(?:iznos\s*za\s*uplatu)',
    ], text)
    res['iznos'] = _to_float(iznos_str)

    # Potrošnja VT / NT (kWh)
    res['kwh_vt'] = _to_float(_search([
        r'(?:vi[šs]a\s*tarifa|VT)[^\d]{0,30}([\d\.,]+)\s*kWh',
        r'tarifa\s*1[^\d]{0,30}([\d\.,]+)\s*kWh',
    ], text))
    res['kwh_nt'] = _to_float(_search([
        r'(?:ni[žz]a\s*tarifa|NT)[^\d]{0,30}([\d\.,]+)\s*kWh',
        r'tarifa\s*2[^\d]{0,30}([\d\.,]+)\s*kWh',
    ], text))
    if res['kwh_vt'] is not None or res['kwh_nt'] is not None:
        res['kwh_plus'] = round((res['kwh_vt'] or 0) + (res['kwh_nt'] or 0), 2)

    # Predaja u mrežu
    res['kwh_minus'] = _to_float(_search([
        r'predaja\s*(?:u\s*)?mre[žz]u[^\d]{0,30}([\d\.,]+)\s*kWh',
        r'isporu[čc]eno[^\d]{0,30}([\d\.,]+)\s*kWh',
    ], text))

    # Stavke računa
    res['opskrba'] = _to_float(_search([
        r'(?:opskrba\s*(?:elektri[čc]nom?\s*energijom?)?|naknada\s*za\s*opskrbu)[^\d€]{0,60}([\d\.,]+)\s*(?:€|EUR)?',
    ], text))
    res['mreza'] = _to_float(_search([
        r'(?:kori[šs]tenje\s*mre[žz]e|naknada\s*za\s*mre[žz]u|distribucija)[^\d€]{0,60}([\d\.,]+)\s*(?:€|EUR)?',
    ], text))
    res['pdv'] = _to_float(_search([
        r'PDV[^\d]{0,30}([\d\.,]+)\s*(?:€|EUR)?',
    ], text))

    return res
