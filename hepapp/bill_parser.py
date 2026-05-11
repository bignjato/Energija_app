"""HEP račun PDF parser.

Kalibriran prema HEPI bijeli tarifnom modelu (E-K-N-BIJ1) — neto mjerenje
kućanstvo. Robusan na varijacije; vraća što je našao + raw text za debug.

Polja:
    period       : "MM/YYYY"
    od / do      : "YYYY-MM-DD"
    iznos        : float (€) — ukupni iznos računa
    kwh_vt       : float — POTROŠNJA viša tarifa (kupljeno)
    kwh_nt       : float — POTROŠNJA niža tarifa
    kwh_plus     : float — ukupna potrošnja (vt+nt)
    kwh_minus    : float — predaja u mrežu (proizvodnja vt+nt)
    opskrba      : float
    mreza        : float
    pdv          : float
"""

import io
import re


def _to_float(s):
    if s is None:
        return None
    s = str(s).strip().replace('€', '').replace('EUR', '').strip()
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
        m = re.search(p, text, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(group)
    return None


def extract_text(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return '\n'.join((page.extract_text() or '') for page in pdf.pages)
    except ImportError:
        raise RuntimeError('pdfplumber nije instaliran u image-u')


def parse_hep_bill(text: str) -> dict:
    res = {
        'period': None, 'od': None, 'do': None,
        'iznos': None,
        'kwh_vt': None, 'kwh_nt': None,
        'kwh_plus': None, 'kwh_minus': None,
        'opskrba': None, 'mreza': None, 'pdv': None,
        # nova polja
        'dug': None,                 # nepodmireno na dan izdavanja računa
        'pretplata': None,           # opskrbna naknada €/mj
        'mjerna_mjernina': None,     # naknada za mjernu uslugu €/mj
        'kamata': None,
        'datum_racuna': None,
        'datum_dospijeca': None,
        'model_tarife': None,        # npr. "HEPI bijeli — E-K-N-BIJ1"
        'sifra_kupca': None,
        'broj_racuna': None,
        'raw_excerpt': text[:2000],
    }

    # --- Period ---
    # "Ukupni iznos računa za 3/2026" (M/YYYY format)
    m = re.search(r'Ukupni\s+iznos\s+ra[čc]una\s+za\s+(\d{1,2})/(\d{4})', text, flags=re.IGNORECASE)
    if m:
        res['period'] = f"{int(m.group(1)):02d}/{m.group(2)}"

    # Razdoblje: "01.03.2026 - 31.03.2026"
    m = re.search(
        r'(\d{1,2})\.(\d{1,2})\.(\d{4})\.?\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.(\d{4})',
        text,
    )
    if m:
        try:
            res['od'] = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            res['do'] = f"{m.group(6)}-{int(m.group(5)):02d}-{int(m.group(4)):02d}"
            if not res['period']:
                res['period'] = f"{int(m.group(2)):02d}/{m.group(3)}"
        except Exception:
            pass

    # --- Ukupni iznos računa ---
    # HEP račun ima dva stupca: lijevo itemization (49,18 + 19,80 + 8,97 + 1,15),
    # desno veliki box "79,10 €". pdfplumber miješa stupce u istu liniju, npr.:
    #   "Ukupni iznos za korištenje mreže i usluga 19,80 € 79,10 €"
    # Strategija 1: dvije X,XX € amounts na istoj liniji → druga je total.
    iznos = None
    m = re.search(
        r'Ukupni\s+iznos\s+ra[čc]una\s+za\s+\d{1,2}/\d{4}([\s\S]{0,800})',
        text, flags=re.IGNORECASE,
    )
    if m:
        block = m.group(1)
        # Traži liniju s dva amount-a: "X,XX € Y,YY €"
        m2 = re.search(r'(\d{1,4}[\.,]\d{2})\s*€\s+(\d{1,4}[\.,]\d{2})\s*€', block)
        if m2:
            iznos = _to_float(m2.group(2))

    # Strategija 2: nedopustivo nisko (parser našao komponentu) → koristi sumu
    if iznos is None or iznos < 5:
        components = [res.get('opskrba'), res.get('mreza'), res.get('pdv')]
        # Kamata (opcionalno)
        kamata = _to_float(_search([
            r'Kamata[^\n]{0,80}?(\d{1,4}[\.,]\d{2})\s*€',
        ], text))
        if kamata:
            components.append(kamata)
        s = sum(c for c in components if c is not None)
        if s > 0:
            iznos = round(s, 2)
    res['iznos'] = iznos

    # --- POTROŠNJA VT/NT ---
    # "viša tarifa - potrošnja    549"
    # "niža tarifa - potrošnja    622"
    res['kwh_vt'] = _to_float(_search([
        r'vi[šs]a\s+tarifa\s*-\s*potro[šs]nja\s+(-?\d+(?:[.,]\d+)?)',
    ], text))
    res['kwh_nt'] = _to_float(_search([
        r'ni[žz]a\s+tarifa\s*-\s*potro[šs]nja\s+(-?\d+(?:[.,]\d+)?)',
    ], text))

    if res['kwh_vt'] is not None or res['kwh_nt'] is not None:
        res['kwh_plus'] = round((res['kwh_vt'] or 0) + (res['kwh_nt'] or 0), 2)

    # --- PROIZVODNJA (predaja u mrežu) VT + NT ---
    vt_proiz = _to_float(_search([
        r'vi[šs]a\s+tarifa\s*-\s*proizvodnja\s+(\d+(?:[.,]\d+)?)',
    ], text))
    nt_proiz = _to_float(_search([
        r'ni[žz]a\s+tarifa\s*-\s*proizvodnja\s+(\d+(?:[.,]\d+)?)',
    ], text))
    if vt_proiz is not None or nt_proiz is not None:
        res['kwh_minus'] = round((vt_proiz or 0) + (nt_proiz or 0), 2)
    else:
        # Fallback: stari format
        res['kwh_minus'] = _to_float(_search([
            r'predaja\s*(?:u\s*)?mre[žz]u[^\d]{0,30}([\d\.,]+)\s*kWh',
            r'predali\s*ste[^\d]{0,30}([\d\.,]+)',
        ], text))

    # --- OPSKRBA (ukupno) ---
    # "Ukupan iznos za opskrbu  49,18"
    # ili "Ukupni iznos za opskrbu   49,18 €"
    res['opskrba'] = _to_float(_search([
        r'Ukupa?n\s+iznos\s+za\s+opskrbu\s+([\d\.,]+)',
    ], text))

    # --- MREŽA (ukupno) ---
    res['mreza'] = _to_float(_search([
        r'Ukupa?n\s+iznos\s+za\s+kori[šs]tenje\s+mre[žz]e(?:\s+i\s+usluga)?\s+([\d\.,]+)',
    ], text))

    # --- PDV ---
    # "PDV 13% (osnovica: 68,98)   8,97 €"
    res['pdv'] = _to_float(_search([
        r'PDV\s*\d*\s*%?\s*\(osnovica:[^)]+\)\s*([\d\.,]+)',
        r'PDV[^\n]{0,40}?([\d\.,]+)\s*€',
    ], text))

    # --- Dug (nepodmireno) ---
    # "Vaš dug 1.054,72 €"
    # ili "Na dan izdavanja računa nepodmireni iznos je 1.054,72 eura"
    res['dug'] = _to_float(_search([
        r'Va[šs]\s*dug[^\d]{0,30}([\d\.,]+)\s*€',
        r'nepodmireni\s*iznos\s*je\s*([\d\.,]+)\s*(?:eur[a]?)',
    ], text))

    # --- Pretplata (opskrbna naknada €/mj) ---
    # "opskrbna naknada po 0,982 EUR/mjesec"
    res['pretplata'] = _to_float(_search([
        r'opskrbna\s*naknada\s*po\s*([\d\.,]+)\s*EUR/mjesec',
    ], text))

    # --- Mjerna mjernina (€/mj) ---
    # "naknada za mjernu uslugu (br.mjeseci) po 1,983 EUR"
    res['mjerna_mjernina'] = _to_float(_search([
        r'naknada\s*za\s*mjernu\s*uslugu[^0-9]{0,40}([\d\.,]+)\s*EUR',
    ], text))

    # --- Kamata ---
    # "Kamata - PDV nije obračunat ... 1,15 €"
    res['kamata'] = _to_float(_search([
        r'Kamata[^\n]{0,80}?([\d\.,]+)\s*€',
    ], text))

    # --- Datum računa & dospijeća ---
    m = re.search(r'Datum\s*ra[čc]una[:\s]*(\d{1,2})\.(\d{1,2})\.(\d{4})', text, flags=re.IGNORECASE)
    if m:
        res['datum_racuna'] = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r'Platite\s*do[:\s]*(\d{1,2})\.(\d{1,2})\.(\d{4})', text, flags=re.IGNORECASE)
    if m:
        res['datum_dospijeca'] = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    # --- Tarifni model ---
    # "Model: HEPI bijeli" + "tarifni model: E-K-N-BIJ1"
    model = _search([r'Model:\s*([A-Za-zČĆŠĐŽčćšđž0-9 \-]+?)(?:\n|tarifni|$)'], text)
    tarif = _search([r'tarifni\s*model:\s*([A-Za-z0-9\-]+)'], text)
    if model and tarif:
        res['model_tarife'] = f"{model.strip()} — {tarif.strip()}"
    elif model:
        res['model_tarife'] = model.strip()
    elif tarif:
        res['model_tarife'] = tarif.strip()

    # --- Šifra kupca i broj računa ---
    res['sifra_kupca'] = _search([r'[ŠS]ifra\s*kupca:\s*([\d]+)'], text)
    res['broj_racuna'] = _search([r'Broj\s*ra[čc]una:\s*([\d\-]+)'], text)

    return res
