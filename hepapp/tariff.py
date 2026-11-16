"""HEP tarifa konstante + procjena računa.

Podržava dva modela:
  - 'standardni' (default): klasično dvotarifno; potrošnja × cijena − predaja × otkup
  - 'bijeli' (HEPI bijeli / E-K-N-BIJ1): net-metering, saldiranje po tarifi mjesečno;
    samo pozitivni saldo se plaća, viškovi otkupljuju zasebnim transferom.

Cijene: ugrađeni cjenik po datumu (TARIFE), preko kojeg idu jedinične cijene
sa zadnjeg uvezenog računa — vidi `get_tarifa`.

VT_UDIO_PERC se može override-ati preko config tablice (key: VT_UDIO_PERC, 0–100).
Tipično: ljeto ~35, prosjek ~45, zima ~55.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime

from . import db as _db
from .db import get_config

log = logging.getLogger(__name__)

# Stavke koje se nisu mijenjale između cjenika (mreža, naknade, PDV).
_ZAJEDNICKO = {
    'vt_distrib':  0.044446,
    'nt_distrib':  0.020514,
    'vt_prijenos': 0.021256,
    'nt_prijenos': 0.008175,
    'solidarna':   0.003982,   # obračunava se pa u cijelosti vraća ("popust na solidarnu naknadu")
    'oie':         0.013239,
    'opskrbna_mj': 0.982,
    'mjerna_mj':   1.983,
    'pdv':         0.13,
    'vt_udio':     0.45,  # default za standardni model (može se promijeniti u Postavkama)
}

# Cjenici po datumu važenja — NAJNOVIJI PRVI. Kad HEP promijeni cijene, dodaj
# novi red na vrh (ne mijenjaj stare: povijesni mjeseci se računaju po cjeniku
# koji je tada vrijedio). Svaki cjenik je provjeren protiv stvarnog računa —
# vidi tests/test_tariff.py (test_stvarni_racun_*).
# Od uvoza prvog računa s novim cijenama vrijede cijene s računa (get_tarifa),
# pa novi red ovdje treba samo za mjesece prije tog računa.
TARIFE = [
    # Od 1.4.2026. — potvrđeno računima 04–10/2026 (npr. 10/2026: 63,39 €).
    ('2026-04-01', {
        **_ZAJEDNICKO,
        'vt_opskrba': 0.077751,
        'nt_opskrba': 0.047688,
        # Otkup VT viška = puna VT cijena opskrbe (račun 10/2026: 546 kWh → 42,45 €).
        'vt_otkup':   0.077751,
        'nt_otkup':   0.047688,   # pretpostavka po analogiji — NT viška na računima još nije bilo
        'otkup':      0.077751,   # standardni model: solarni višak nastaje u VT satima
    }),
    # Do 31.3.2026. — potvrđeno računima 01–03/2026 (npr. 03/2026: opskrba 49,18 €).
    ('1900-01-01', {
        **_ZAJEDNICKO,
        'vt_opskrba': 0.131205,
        'nt_opskrba': 0.064379,
        'vt_otkup':   0.104964,
        'nt_otkup':   0.064379,
        'otkup':      0.064379,
    }),
]


# --- Cijene s uvezenih računa ------------------------------------------------
# Ugrađeni cjenik je samo polazište: jedinične cijene otisnute na zadnjem
# uvezenom računu (racuni.cijene_json, puni ga bill_parser) ga prekrivaju, pa
# iduća HEP-ova promjena cijena ne traži izmjenu koda — dovoljno je uvesti račun.

# Ključevi koje račun smije prekriti. 'pdv' i 'vt_udio' nisu cijene s računa.
_PO_KWH = ('vt_opskrba', 'nt_opskrba', 'vt_distrib', 'nt_distrib',
           'vt_prijenos', 'nt_prijenos', 'solidarna', 'oie',
           'vt_otkup', 'nt_otkup', 'otkup')
_MJESECNE = ('opskrbna_mj', 'mjerna_mj')

_CACHE_TTL = 60.0   # s — i sync kontejner unutar minute vidi novi račun
_cache = {'do': None, 'racuni': []}   # racuni: [(period 'YYYY-MM', {ključ: cijena})], najnoviji prvi
_cache_lock = threading.Lock()
_upozoreno = set()


def invalidate_tarifa_cache():
    """Zaboravi učitane cijene računa (poziva se nakon spremanja/brisanja računa)."""
    with _cache_lock:
        _cache['do'] = None
        _cache['racuni'] = []


def _norm_period(p):
    """'YYYY-MM' (ili stari zapis 'MM/YYYY') → 'YYYY-MM'; inače None."""
    p = str(p or '').strip()
    if re.fullmatch(r'\d{4}-\d{2}', p):
        return p
    m = re.fullmatch(r'(\d{1,2})/(\d{4})', p)
    if m:
        return f'{m.group(2)}-{int(m.group(1)):02d}'
    return None


def _provjeri_cijene(period, sirovo) -> dict:
    """Zadrži samo poznate ključeve s razumnom vrijednošću; ostalo ignoriraj uz jedno upozorenje."""
    cijene = {}
    if not isinstance(sirovo, dict):
        return cijene
    for k, v in sirovo.items():
        if k in _PO_KWH:
            gornja = 1.0
        elif k in _MJESECNE:
            gornja = 50.0
        else:
            continue
        try:
            if isinstance(v, bool):
                raise ValueError
            f = float(v)
        except (TypeError, ValueError):
            f = None
        if f is not None and 0 < f < gornja:   # NaN pada ovdje
            cijene[k] = f
        elif (period, k) not in _upozoreno:
            _upozoreno.add((period, k))
            log.warning('Račun %s: cijena %s=%r izvan razumnog raspona — ignoriram '
                        '(vrijedi prethodni račun ili ugrađeni cjenik)', period, k, v)
    return cijene


def _ucitaj_cijene_racuna() -> list:
    """Cijene svih računa koji ih imaju (kratko keširano). Nikad ne baca iznimku:
    nema baze / tablice / kolone ili je baza zaključana → prazna lista."""
    now = time.monotonic()
    with _cache_lock:
        if _cache['do'] is not None and now < _cache['do']:
            return _cache['racuni']
    racuni = []
    try:
        # get_db() bi nepostojeću bazu kreirao praznu — standalone skripte to ne smiju
        if os.path.exists(_db.DB_PATH):
            conn = _db.get_db()
            try:
                rows = conn.execute(
                    'SELECT period, cijene_json FROM racuni WHERE cijene_json IS NOT NULL'
                ).fetchall()
            finally:
                conn.close()
            for period_raw, cj in rows:
                period = _norm_period(period_raw)
                if not period:
                    continue
                try:
                    cijene = _provjeri_cijene(period, json.loads(cj))
                except (TypeError, ValueError):
                    continue
                if cijene:
                    racuni.append((period, cijene))
            racuni.sort(key=lambda r: r[0], reverse=True)
    except Exception as e:
        log.debug('Cijene računa nedostupne (%s) — ugrađeni cjenik', e)
        racuni = []
    with _cache_lock:
        _cache['racuni'] = racuni
        _cache['do'] = time.monotonic() + _CACHE_TTL
    return racuni


def _datum_str(datum) -> str:
    if datum is None:
        return date.today().isoformat()
    if isinstance(datum, (date, datetime)):
        return datum.strftime('%Y-%m-%d')
    d = str(datum)[:10]
    if len(d) == 7:
        d += '-01'
    return d


def _ugradjeni(d: str):
    """(vrijedi_od, cjenik) ugrađenog cjenika koji vrijedi na datum d."""
    for vrijedi_od, cjenik in TARIFE:
        if d >= vrijedi_od:
            return vrijedi_od, cjenik
    return TARIFE[-1]


def _racun_za(d: str, vrijedi_od: str):
    """(period, cijene) s računa koje vrijede na datum d, ili None.

    Gledaju se računi s cijenama od mjeseca važenja ugrađenog cjenika do
    uključivo mjeseca datuma — račun iz starog cjenovnog režima ne smije
    prekriti noviji ugrađeni cjenik. Svaki ključ se uzima s najnovijeg računa
    koji ga ima: stavka koje na zadnjem računu nema (npr. otkup u mjesecu bez
    viška) ostaje po prethodnom računu, a ne pada na ugrađeni cjenik.
    Period je onaj najnovijeg računa.
    """
    mjesec = d[:7]
    u_rezimu = [r for r in _ucitaj_cijene_racuna()
                if vrijedi_od[:7] <= r[0] <= mjesec]   # najnoviji prvi
    if not u_rezimu:
        return None
    cijene = {}
    for _, c in reversed(u_rezimu):
        cijene.update(c)
    return u_rezimu[0][0], cijene


def get_tarifa(datum=None) -> dict:
    """Cjenik koji vrijedi na zadani datum.

    datum: None (danas), 'YYYY-MM-DD', 'YYYY-MM' (uzima se 1. u mjesecu),
           ili date/datetime objekt.

    Redoslijed: ugrađeni cjenik za taj datum (TARIFE), preko njega cijene s
    uvezenih računa do tog mjeseca, novije preko starijih (vidi `_racun_za`). Uvijek vraća
    novi dict s istim ključevima; zbog baze nikad ne baca iznimku.
    """
    d = _datum_str(datum)
    vrijedi_od, cjenik = _ugradjeni(d)
    t = dict(cjenik)
    racun = _racun_za(d, vrijedi_od)
    if racun:
        t.update(racun[1])
    return t


def get_tarifa_izvor(datum=None) -> str:
    """Odakle su cijene za zadani datum: 'račun 2026-10' ili 'ugrađeni cjenik od 2026-04-01'."""
    d = _datum_str(datum)
    vrijedi_od, _ = _ugradjeni(d)
    racun = _racun_za(d, vrijedi_od)
    if racun:
        return f'račun {racun[0]}'
    return f'ugrađeni cjenik od {vrijedi_od}'


class _TrenutnaTarifa(dict):
    """HEP_TARIFA — read-only pogled na `get_tarifa()` (cjenik koji vrijedi danas).

    Svako čitanje ide kroz get_tarifa(), pa pozivatelji koji drže
    `HEP_TARIFA['...']` vide trenutne cijene bez izmjene koda.

    Nasljeđuje dict (a ne samo Mapping) jer ga pozivatelji šalju ravno u
    jsonify, a json enkoder prima samo dict; vlastiti sadržaj dict-a se pri
    svakom čitanju osvježi da i C prečaci (json, dict.copy) vide iste cijene.
    """
    __slots__ = ()

    def __init__(self):
        dict.__init__(self, TARIFE[0][1])

    def _t(self):
        t = get_tarifa()
        dict.update(self, t)
        return t

    def __getitem__(self, k):
        return self._t()[k]

    def get(self, k, default=None):
        return self._t().get(k, default)

    def __contains__(self, k):
        return k in self._t()

    def __iter__(self):
        return iter(self._t())

    def __len__(self):
        return len(self._t())

    def keys(self):
        return self._t().keys()

    def items(self):
        return self._t().items()

    def values(self):
        return self._t().values()

    def copy(self):
        return self._t()

    def __eq__(self, other):
        return self._t() == other

    def __ne__(self, other):
        return not self.__eq__(other)

    def __or__(self, other):
        return self._t() | other

    def __ror__(self, other):
        return other | self._t()

    def __repr__(self):
        return f'HEP_TARIFA({self._t()!r})'

    def __reduce__(self):
        return (dict, (self._t(),))

    def _samo_citanje(self, *a, **kw):
        raise TypeError('HEP_TARIFA je samo za čitanje — cijene dolaze iz get_tarifa()')

    __setitem__ = __delitem__ = __ior__ = _samo_citanje
    clear = pop = popitem = setdefault = update = _samo_citanje


# Trenutno važeći cjenik (kompatibilnost sa starim pozivateljima).
HEP_TARIFA = _TrenutnaTarifa()


def get_vt_udio() -> float:
    """Dohvati trenutni VT udio (0–1). Default 0.45."""
    try:
        v = float(get_config('VT_UDIO_PERC', '45'))
        if 0 <= v <= 100:
            return v / 100.0
    except Exception:
        pass
    return 0.45


def get_vt_udio_registri(conn):
    """Sluzbeni VT udjeli iz HEP registara (delta mjesecnih stanja brojila).

    Vraca (po_mjesecu, prosjek):
      po_mjesecu: {'YYYY-MM': vt_udio 0-1} za svaki mjesec s oba stanja
      prosjek:    potrosnjom vagani prosjek zadnja 3 mjeseca (ili None)
    """
    try:
        rows = conn.execute(
            "SELECT datum, obis, value FROM hep_registri "
            "WHERE obis IN ('A+_T1','A+_T2') ORDER BY datum"
        ).fetchall()
    except Exception:
        return {}, None
    stanja = {}
    for r in rows:
        stanja.setdefault(r['datum'], {})[r['obis']] = r['value']
    datumi = sorted(d for d, v in stanja.items()
                    if v.get('A+_T1') is not None and v.get('A+_T2') is not None)
    po_mjesecu = {}
    delte = []
    for prev, cur in zip(datumi, datumi[1:]):
        dvt = stanja[cur]['A+_T1'] - stanja[prev]['A+_T1']
        dnt = stanja[cur]['A+_T2'] - stanja[prev]['A+_T2']
        uk = dvt + dnt
        if dvt < 0 or dnt < 0 or uk <= 0:
            continue  # zamjena brojila i sl.
        po_mjesecu[cur[:7]] = dvt / uk
        delte.append((dvt, uk))
    prosjek = None
    zadnje = delte[-3:]
    if zadnje:
        sum_vt = sum(d[0] for d in zadnje)
        sum_uk = sum(d[1] for d in zadnje)
        if sum_uk > 0:
            prosjek = sum_vt / sum_uk
    return po_mjesecu, prosjek


def get_tariff_model() -> str:
    """Vrati 'bijeli' ili 'standardni' iz config-a."""
    return (get_config('TARIFA_MODEL', 'standardni') or 'standardni').lower()


def is_bijeli(conn=None) -> bool:
    """Je li kupac na HEPI bijelom (net-metering) modelu — JEDINO mjesto detekcije.

    Redoslijed: model_tarife s najnovijeg uvezenog računa koji ga ima (sadrži
    'bijeli' ili 'BIJ', npr. 'HEPI bijeli — E-K-N-BIJ1') → inače config
    TARIFA_MODEL == 'bijeli' → inače False.
    conn: otvorena konekcija (ne zatvara se); None = otvori vlastitu.
    Nikad ne baca iznimku (nema baze / tablice / kolone → gleda se samo config).
    """
    model = ''
    try:
        vlastita = conn is None
        if vlastita:
            # get_db() bi nepostojeću bazu kreirao praznu (vidi _ucitaj_cijene_racuna)
            conn = _db.get_db() if os.path.exists(_db.DB_PATH) else None
        if conn is not None:
            try:
                rows = conn.execute(
                    "SELECT period, model_tarife FROM racuni "
                    "WHERE model_tarife IS NOT NULL AND TRIM(model_tarife) != ''"
                ).fetchall()
            finally:
                if vlastita:
                    conn.close()
            if rows:
                # period može biti i stari zapis 'MM/YYYY' -> sortiraj normalizirano
                model = max(rows, key=lambda r: _norm_period(r[0]) or '')[1] or ''
    except Exception as e:
        log.debug('Model tarife s računa nedostupan (%s) — gledam config', e)
    if 'bij' in model.lower():
        return True
    return get_tariff_model() == 'bijeli'


def get_model_tarife(conn=None) -> str:
    """'bijeli' ili 'standardni' — vidi `is_bijeli`."""
    return 'bijeli' if is_bijeli(conn) else 'standardni'


def zadnji_racun_i_dug(conn):
    """(zadnji uvezeni račun, zadnji račun s poznatim dugom) kao dict ili None.

    JEDINO mjesto koje određuje "trenutni račun" i "trenutni dug" — koriste ga
    /api/stats/sazetak i /api/stats/dug da svi tabovi pokažu isti dug.
    "Zadnji" = najveći normalizirani period (ne datum_racuna: ručno uneseni
    račun nema ni datum_racuna ni dug). Ručno uneseni račun zato NE briše dug:
    dug i dospijeće ostaju s najnovijeg računa koji ih ima.
    Nikad ne baca iznimku (nema tablice / kolone → (None, None)).
    """
    try:
        rows = [dict(r) for r in conn.execute('SELECT * FROM racuni').fetchall()]
    except Exception as e:
        log.debug('Računi nedostupni (%s)', e)
        return None, None
    rows.sort(key=lambda r: _norm_period(r.get('period')) or '')
    zadnji = rows[-1] if rows else None
    s_dugom = next((r for r in reversed(rows) if r.get('dug') is not None), None)
    return zadnji, s_dugom


def izracunaj_racun(kwh_plus, kwh_minus, n_dana=30, vt_udio=None, datum=None):
    """Standardni dvotarifni model — približna formula s VT_UDIO slider-om.

    datum: po kojem cjeniku računati (None = trenutni); vidi `get_tarifa`.
    """
    t = get_tarifa(datum)
    if vt_udio is None:
        vt_udio = get_vt_udio()
    vt = kwh_plus * vt_udio
    nt = kwh_plus * (1 - vt_udio)
    n_mj = n_dana / 30.0
    opskrba = (vt * t['vt_opskrba'] + nt * t['nt_opskrba'] +
               kwh_plus * (t['solidarna'] + t['oie']) +
               t['opskrbna_mj'] * n_mj -
               kwh_minus * t['otkup'])
    # Popust na solidarnu naknadu — na računu se obračuna pa u cijelosti vrati.
    opskrba -= kwh_plus * t['solidarna']
    mreza = (vt * (t['vt_distrib'] + t['vt_prijenos']) +
             nt * (t['nt_distrib'] + t['nt_prijenos']) +
             t['mjerna_mj'] * n_mj)
    return round((opskrba + mreza) * (1 + t['pdv']), 2)


def izracunaj_racun_bijeli(vt_pot, nt_pot, vt_pred, nt_pred, n_dana=30, datum=None):
    """HEPI bijeli (net-metering) formula.

    datum: po kojem cjeniku računati (None = trenutni); vidi `get_tarifa`.

    Mjesečno saldiranje po tarifi:
        saldo_vt = vt_pot - vt_pred   (pozitivno = neto kupili VT)
        saldo_nt = nt_pot - nt_pred   (pozitivno = neto kupili NT)

    Plaća se SAMO pozitivni saldo. Negativni saldo se otkupljuje
    posebnim transferom (ne ulazi u račun); vidi `otkup_viskova_bijeli`.

    Vraća dict:
        iznos       — ukupno za platiti (osnovica + PDV, bez kamata)
        osnovica    — opskrba + mreža
        opskrba, mreza, pdv  — komponente
        saldo_vt, saldo_nt
    """
    t = get_tarifa(datum)
    saldo_vt = (vt_pot or 0) - (vt_pred or 0)
    saldo_nt = (nt_pot or 0) - (nt_pred or 0)
    pos_vt = max(0, saldo_vt)
    pos_nt = max(0, saldo_nt)
    n_mj = n_dana / 30.0

    opskrba = (
        pos_vt * t['vt_opskrba'] +
        pos_nt * t['nt_opskrba'] +
        (pos_vt + pos_nt) * (t['solidarna'] + t['oie']) +
        t['opskrbna_mj'] * n_mj
    )
    # Popust na solidarnu (vidi PDF: "popust na solidarnu naknadu") — simetrično je s naknadom
    popust_solidarna = -(pos_vt + pos_nt) * t['solidarna']
    opskrba += popust_solidarna

    mreza = (
        pos_vt * (t['vt_distrib'] + t['vt_prijenos']) +
        pos_nt * (t['nt_distrib'] + t['nt_prijenos']) +
        t['mjerna_mj'] * n_mj
    )

    osnovica = opskrba + mreza
    pdv = osnovica * t['pdv']
    iznos = round(osnovica + pdv, 2)

    return {
        'iznos':    iznos,
        'osnovica': round(osnovica, 2),
        'opskrba':  round(opskrba, 2),
        'mreza':    round(mreza, 2),
        'pdv':      round(pdv, 2),
        'saldo_vt': round(saldo_vt, 2),
        'saldo_nt': round(saldo_nt, 2),
    }


def otkup_viskova_bijeli(vt_pot, nt_pot, vt_pred, nt_pred, datum=None):
    """Otkup za HEPI bijeli — viškovi (negativni saldo) × otkupna cijena.

    datum: po kojem cjeniku računati (None = trenutni); vidi `get_tarifa`.
    Vraća dict: vt_kwh, nt_kwh, vt_eur, nt_eur, ukupno_eur.
    """
    t = get_tarifa(datum)
    vt_visak = max(0, (vt_pred or 0) - (vt_pot or 0))
    nt_visak = max(0, (nt_pred or 0) - (nt_pot or 0))
    vt_eur = round(vt_visak * t['vt_otkup'], 2)
    nt_eur = round(nt_visak * t['nt_otkup'], 2)
    return {
        'vt_kwh':     vt_visak,
        'nt_kwh':     nt_visak,
        'vt_eur':     vt_eur,
        'nt_eur':     nt_eur,
        'ukupno_eur': round(vt_eur + nt_eur, 2),
    }
