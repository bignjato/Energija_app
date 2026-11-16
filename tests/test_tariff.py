"""Testovi izracuna racuna (standardni + HEPI bijeli net-metering)."""

import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import date

import pytest

import hepapp.db as db
from hepapp import tariff
from hepapp.tariff import (
    HEP_TARIFA,
    TARIFE,
    get_tarifa,
    get_tarifa_izvor,
    get_vt_udio_registri,
    invalidate_tarifa_cache,
    izracunaj_racun,
    izracunaj_racun_bijeli,
    otkup_viskova_bijeli,
)


@pytest.fixture(autouse=True)
def _bez_baze(monkeypatch, tmp_path):
    # svaki test krece bez baze (cisti ugradjeni cjenik) i s praznim kesom;
    # testovi prekrivanja cijenama racuna bazu naprave fixtureom `baza`
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'nema.db'))
    invalidate_tarifa_cache()
    yield
    invalidate_tarifa_cache()


@pytest.fixture
def baza(monkeypatch, tmp_path):
    """Baza s tablicom racuni; vraca funkciju dodaj(period, cijene)."""
    path = str(tmp_path / 'hep.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE racuni (period TEXT UNIQUE, iznos REAL, cijene_json TEXT)')
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, 'DB_PATH', path)

    def dodaj(period, cijene):
        c = sqlite3.connect(path)
        c.execute('INSERT OR REPLACE INTO racuni (period, iznos, cijene_json) VALUES (?,?,?)',
                  (period, 50.0, cijene if cijene is None or isinstance(cijene, str)
                   else json.dumps(cijene)))
        c.commit()
        c.close()
        invalidate_tarifa_cache()
    dodaj.path = path
    return dodaj


def test_izracunaj_racun_standardni():
    # 1000 kWh, bez predaje, 30 dana, VT udio 45% — rucno izracunate vrijednosti
    # (solidarna naknada se obracuna pa u cijelosti vrati -> ne ulazi u iznos)
    # stari cjenik (do 31.3.2026.)
    assert izracunaj_racun(1000, 0, 30, vt_udio=0.45, datum='2026-01-15') == pytest.approx(176.28, abs=0.01)
    # cjenik od 1.4.2026.
    assert izracunaj_racun(1000, 0, 30, vt_udio=0.45, datum='2026-10-15') == pytest.approx(138.72, abs=0.01)


def test_standardni_solidarna_se_vraca():
    # iznos ne smije ovisiti o visini solidarne naknade (popust je simetrican)
    t = get_tarifa('2026-10-15')
    bez_naknada = (450 * (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos']) +
                   550 * (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos']) +
                   1000 * t['oie'] + t['opskrbna_mj'] + t['mjerna_mj'])
    assert izracunaj_racun(1000, 0, 30, vt_udio=0.45, datum='2026-10-15') == \
        pytest.approx(bez_naknada * (1 + t['pdv']), abs=0.01)


# ---------------------------------------------------------------------------
# Cjenici po datumu
# ---------------------------------------------------------------------------

def test_get_tarifa_po_datumu():
    assert get_tarifa('2026-03-31')['nt_opskrba'] == 0.064379
    assert get_tarifa('2026-04-01')['nt_opskrba'] == 0.047688
    assert get_tarifa('2026-03')['vt_opskrba'] == 0.131205      # 'YYYY-MM'
    assert get_tarifa('2026-04')['vt_opskrba'] == 0.077751
    assert get_tarifa('2019-01-01') == TARIFE[-1][1]            # prije svega -> najstariji
    assert get_tarifa('2019-01-01') is not TARIFE[-1][1]        # kopija — pozivatelj ne moze pokvariti cjenik
    assert get_tarifa(date(2026, 10, 31))['vt_otkup'] == 0.077751


def test_tarife_su_sortirane_i_potpune():
    datumi = [d for d, _ in TARIFE]
    assert datumi == sorted(datumi, reverse=True), 'TARIFE mora biti najnoviji prvi'
    kljucevi = set(TARIFE[0][1])
    for d, cjenik in TARIFE:
        assert set(cjenik) == kljucevi, f'cjenik {d} nema iste kljuceve'
    # HEP_TARIFA vise nije zamrznuti dict nego pogled na get_tarifa()
    assert dict(HEP_TARIFA) == get_tarifa()
    assert set(HEP_TARIFA) == kljucevi


# ---------------------------------------------------------------------------
# Cijene s uvezenih racuna prekrivaju ugradjeni cjenik
# ---------------------------------------------------------------------------

def test_racun_za_isti_mjesec_pobjedjuje(baza):
    baza('2026-10', {'vt_opskrba': 0.09, 'opskrbna_mj': 1.1})
    t = get_tarifa('2026-10-15')
    assert t['vt_opskrba'] == 0.09
    assert t['opskrbna_mj'] == 1.1
    assert t['nt_opskrba'] == 0.047688            # cega nema na racunu ostaje iz ugradjenog
    assert set(t) == set(TARIFE[0][1])            # isti kljucevi kao i uvijek
    assert get_tarifa_izvor('2026-10-15') == 'račun 2026-10'
    # mjesec prije racuna ne dira
    assert get_tarifa('2026-09')['vt_opskrba'] == 0.077751
    assert get_tarifa_izvor('2026-09') == 'ugrađeni cjenik od 2026-04-01'


def test_kasniji_mjesec_nasljedjuje_zadnji_raniji_racun(baza):
    baza('2026-08', {'vt_opskrba': 0.08})
    baza('2026-10', {'vt_opskrba': 0.09})
    assert get_tarifa('2026-09-30')['vt_opskrba'] == 0.08
    assert get_tarifa('2026-12')['vt_opskrba'] == 0.09
    assert get_tarifa(date(2027, 3, 1))['vt_opskrba'] == 0.09
    assert get_tarifa_izvor('2026-12') == 'račun 2026-10'
    # racun bez cijena (rucni unos) se preskace
    baza('2026-11', None)
    assert get_tarifa('2026-12')['vt_opskrba'] == 0.09


def test_kljuc_kojeg_nema_na_zadnjem_racunu_ostaje_s_prethodnog(baza):
    baza('2026-08', {'vt_opskrba': 0.08, 'vt_otkup': 0.081, 'oie': 0.014})
    baza('2026-10', {'vt_opskrba': 0.09})           # mjesec bez viska -> nema vt_otkup
    t = get_tarifa('2026-11')
    assert t['vt_opskrba'] == 0.09                  # noviji racun pobjedjuje
    assert t['vt_otkup'] == 0.081                   # s prethodnog racuna, ne ugradjeni 0.077751
    assert t['oie'] == 0.014
    assert t['nt_opskrba'] == 0.047688              # nema ni na jednom -> ugradjeni
    assert set(t) == set(TARIFE[0][1])
    assert get_tarifa_izvor('2026-11') == 'račun 2026-10'
    # mjesec izmedju vidi samo stariji racun
    assert get_tarifa('2026-09')['vt_opskrba'] == 0.08
    # nerazumna vrijednost na novijem racunu ne brise dobru s prethodnog
    baza('2026-11', {'oie': 13.239})
    assert get_tarifa('2026-12')['oie'] == 0.014
    assert get_tarifa_izvor('2026-12') == 'račun 2026-10'   # 11/2026 nema nijednu valjanu cijenu


def test_nasljedjivanje_kljuceva_ne_prelazi_granicu_rezima(baza):
    baza('2026-02', {'vt_otkup': 0.2, 'vt_opskrba': 0.2})
    baza('2026-10', {'vt_opskrba': 0.09})
    t = get_tarifa('2026-11')
    assert t['vt_opskrba'] == 0.09
    assert t['vt_otkup'] == 0.077751                # racun 02/2026 je iz starog rezima


def test_racun_iz_starog_rezima_ne_prekriva_noviji_cjenik(baza):
    baza('2026-02', {'vt_opskrba': 0.2, 'nt_opskrba': 0.1})
    # unutar starog rezima racun vrijedi ...
    assert get_tarifa('2026-03')['vt_opskrba'] == 0.2
    # ... ali od 1.4.2026. vrijedi noviji ugradjeni cjenik
    assert get_tarifa('2026-04-01') == TARIFE[0][1]
    assert get_tarifa('2026-10') == TARIFE[0][1]
    assert get_tarifa_izvor('2026-10') == 'ugrađeni cjenik od 2026-04-01'
    assert get_tarifa_izvor('2026-03') == 'račun 2026-02'


def test_period_u_starom_zapisu_mm_yyyy(baza):
    baza('10/2026', {'vt_opskrba': 0.09})
    assert get_tarifa('2026-11')['vt_opskrba'] == 0.09


def test_nerazumne_cijene_se_odbacuju(baza, caplog):
    baza('2026-10', {
        'vt_opskrba': 77.751,      # EUR/MWh umjesto EUR/kWh
        'nt_opskrba': 0,
        'vt_distrib': -0.04,
        'oie': 'abc',
        'opskrbna_mj': 98.2,       # mjesecna naknada > 50
        'mjerna_mj': 2.5,          # u redu
        'solidarna': 0.004,        # u redu
        'pdv': 0.25,               # nije cijena s racuna — ne prekriva se
        'nepoznato': 0.5,
    })
    with caplog.at_level(logging.WARNING, logger='hepapp.tariff'):
        t = get_tarifa('2026-10')
        ugr = TARIFE[0][1]
        for k in ('vt_opskrba', 'nt_opskrba', 'vt_distrib', 'oie', 'opskrbna_mj', 'pdv'):
            assert t[k] == ugr[k], k
        assert t['mjerna_mj'] == 2.5
        assert t['solidarna'] == 0.004
        assert 'nepoznato' not in t
        n = len([r for r in caplog.records if 'vt_opskrba' in r.getMessage()])
        assert n == 1
        # upozorenje samo jednom, i nakon ponovnog ucitavanja
        invalidate_tarifa_cache()
        get_tarifa('2026-10')
        assert len([r for r in caplog.records if 'vt_opskrba' in r.getMessage()]) == 1


def test_pokvaren_json_se_ignorira(baza):
    baza('2026-10', '{nije json')
    baza('2026-09', '[1, 2]')
    assert get_tarifa('2026-10') == TARIFE[0][1]


def test_bez_baze_ugradjeni_cjenik(tmp_path):
    # _bez_baze: DB_PATH pokazuje na nepostojecu datoteku
    assert get_tarifa('2026-10') == TARIFE[0][1]
    assert get_tarifa_izvor('2026-10') == 'ugrađeni cjenik od 2026-04-01'
    assert get_tarifa_izvor('2026-01') == 'ugrađeni cjenik od 1900-01-01'
    assert not (tmp_path / 'nema.db').exists()    # citanje cjenika ne smije stvoriti bazu


def test_baza_bez_tablice_ili_kolone_ne_puca(monkeypatch, tmp_path):
    path = str(tmp_path / 'stara.db')
    conn = sqlite3.connect(path)
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, 'DB_PATH', path)
    invalidate_tarifa_cache()
    assert get_tarifa('2026-10') == TARIFE[0][1]          # nema tablice racuni
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE racuni (period TEXT, iznos REAL)')
    conn.execute("INSERT INTO racuni VALUES ('2026-10', 63.39)")
    conn.commit()
    conn.close()
    invalidate_tarifa_cache()
    assert get_tarifa('2026-10') == TARIFE[0][1]          # nema kolone cijene_json
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path))     # direktorij, ne baza
    invalidate_tarifa_cache()
    assert get_tarifa('2026-10') == TARIFE[0][1]


def test_kes_i_invalidate(baza, monkeypatch):
    baza('2026-10', {'vt_opskrba': 0.09})
    assert get_tarifa('2026-10')['vt_opskrba'] == 0.09
    # izmjena mimo aplikacije: kes jos drzi staru cijenu ...
    conn = sqlite3.connect(baza.path)
    conn.execute("UPDATE racuni SET cijene_json=? WHERE period='2026-10'",
                 (json.dumps({'vt_opskrba': 0.1}),))
    conn.commit()
    conn.close()
    assert get_tarifa('2026-10')['vt_opskrba'] == 0.09
    # ... do invalidate_tarifa_cache()
    invalidate_tarifa_cache()
    assert get_tarifa('2026-10')['vt_opskrba'] == 0.1
    # ... ili do isteka TTL-a
    conn = sqlite3.connect(baza.path)
    conn.execute("DELETE FROM racuni")
    conn.commit()
    conn.close()
    assert get_tarifa('2026-10')['vt_opskrba'] == 0.1
    sada = tariff.time.monotonic()
    monkeypatch.setattr(tariff.time, 'monotonic', lambda: sada + tariff._CACHE_TTL + 1)
    assert get_tarifa('2026-10')['vt_opskrba'] == 0.077751


def test_stvarni_racun_10_2026_s_cijenama_s_racuna(baza):
    # cijene kako ih bill_parser procita s racuna 10/2026 — izracun mora i dalje
    # pogoditi stvarni racun i stvarni otkup
    baza('2026-10', {
        'vt_opskrba': 0.077751, 'nt_opskrba': 0.047688,
        'vt_distrib': 0.044446, 'nt_distrib': 0.020514,
        'vt_prijenos': 0.021256, 'nt_prijenos': 0.008175,
        'solidarna': 0.003982, 'oie': 0.013239,
        'opskrbna_mj': 0.982, 'mjerna_mj': 1.983,
        'vt_otkup': 0.077747, 'otkup': 0.077747,
    })
    r = izracunaj_racun_bijeli(533, 595, 1079, 2, 30, datum='2026-10')
    assert r['iznos'] == pytest.approx(63.39, abs=0.05)
    o = otkup_viskova_bijeli(533, 595, 1079, 2, datum='2026-10')
    assert o['ukupno_eur'] == pytest.approx(42.45, abs=0.01)
    assert get_tarifa('2026-10')['nt_otkup'] == 0.047688   # NT otkup ostaje ugradjeni


# ---------------------------------------------------------------------------
# HEP_TARIFA — read-only pogled na get_tarifa()
# ---------------------------------------------------------------------------

def test_hep_tarifa_mapping():
    t = get_tarifa()
    assert isinstance(HEP_TARIFA, Mapping)
    assert HEP_TARIFA['vt_opskrba'] == t['vt_opskrba']
    assert HEP_TARIFA.get('otkup', 1) == t['otkup']
    assert HEP_TARIFA.get('nema') is None
    assert HEP_TARIFA.get('nema', 5) == 5
    assert 'pdv' in HEP_TARIFA and 'nema' not in HEP_TARIFA
    assert len(HEP_TARIFA) == len(t)
    assert list(HEP_TARIFA) == list(t)
    assert list(HEP_TARIFA.keys()) == list(t.keys())
    assert dict(HEP_TARIFA.items()) == t
    assert sorted(HEP_TARIFA.values()) == sorted(t.values())
    assert dict(HEP_TARIFA) == t
    assert {**HEP_TARIFA} == t
    assert HEP_TARIFA == t
    assert json.loads(json.dumps(HEP_TARIFA)) == t
    assert json.loads(json.dumps(HEP_TARIFA, indent=2)) == t
    with pytest.raises(KeyError):
        HEP_TARIFA['nema']


def test_hep_tarifa_je_samo_za_citanje():
    with pytest.raises(TypeError):
        HEP_TARIFA['vt_opskrba'] = 1
    with pytest.raises(TypeError):
        del HEP_TARIFA['vt_opskrba']
    with pytest.raises(TypeError):
        HEP_TARIFA.update({'pdv': 0})
    with pytest.raises(TypeError):
        HEP_TARIFA.pop('pdv')
    kopija = HEP_TARIFA.copy()
    kopija['pdv'] = 0                 # kopija je obican dict
    assert HEP_TARIFA['pdv'] == 0.13


def test_hep_tarifa_prati_uvezeni_racun(baza):
    mj = date.today().strftime('%Y-%m')
    prije = HEP_TARIFA['vt_opskrba']
    baza(mj, {'vt_opskrba': 0.0912, 'mjerna_mj': 2.1})
    assert prije != 0.0912
    assert HEP_TARIFA['vt_opskrba'] == 0.0912
    assert dict(HEP_TARIFA)['mjerna_mj'] == 2.1
    assert json.loads(json.dumps(HEP_TARIFA))['vt_opskrba'] == 0.0912
    assert json.loads(json.dumps({'tarifa': HEP_TARIFA}))['tarifa']['mjerna_mj'] == 2.1


def test_hep_tarifa_kroz_flask_jsonify(baza):
    # stats.py salje HEP_TARIFA ravno u jsonify
    from flask import Flask, jsonify
    mj = date.today().strftime('%Y-%m')
    baza(mj, {'nt_opskrba': 0.0555})
    app = Flask(__name__)
    with app.app_context():
        d = jsonify({'tarifa': HEP_TARIFA}).get_json()
    assert d['tarifa'] == get_tarifa()
    assert d['tarifa']['nt_opskrba'] == 0.0555


# ---------------------------------------------------------------------------
# Regresija protiv STVARNIH HEP racuna (model HEPI bijeli, OMM 0149216862).
# Ako ovi testovi padnu nakon izmjene cijena/formule — izracun se razisao
# od onoga sto HEP stvarno naplacuje. Tolerancija 5 centi pokriva HEP-ovo
# zaokruzivanje po stavci.
# ---------------------------------------------------------------------------

STVARNI_RACUNI = [
    # (period, vt_pot, nt_pot, vt_pred, nt_pred, opskrba, mreza, iznos)
    # 03/2026 — zadnji mjesec starog cjenika. `iznos` na racunu (79,10) ukljucuje
    # i stavku izvan opskrbe/mreze, pa se ovdje provjeravaju samo komponente.
    ('2026-03', 549, 622, 1507, 1, 49.18, 19.80, None),
    # 06/2026 — novi cjenik
    ('2026-06', 295, 480, 2740, 125, 22.61, 12.16, 39.29),
    # 10/2026 — novi cjenik; racun br. 0010288297-261020-8
    ('2026-10', 533, 595, 1079, 2, 37.11, 18.99, 63.39),
]


@pytest.mark.parametrize('period,vt_pot,nt_pot,vt_pred,nt_pred,opskrba,mreza,iznos', STVARNI_RACUNI)
def test_stvarni_racun_bijeli(period, vt_pot, nt_pot, vt_pred, nt_pred, opskrba, mreza, iznos):
    r = izracunaj_racun_bijeli(vt_pot, nt_pot, vt_pred, nt_pred, 30, datum=period)
    assert r['opskrba'] == pytest.approx(opskrba, abs=0.05)
    assert r['mreza'] == pytest.approx(mreza, abs=0.05)
    if iznos is not None:
        assert r['iznos'] == pytest.approx(iznos, abs=0.05)


def test_stvarni_otkup_listopad_2026():
    # "U listopadu smo od Vas otkupili 546 kWh elektricne energije - za 42,45 eura."
    o = otkup_viskova_bijeli(533, 595, 1079, 2, datum='2026-10')
    assert o['vt_kwh'] == 546
    assert o['nt_kwh'] == 0
    assert o['ukupno_eur'] == pytest.approx(42.45, abs=0.01)


def test_stari_cjenik_bi_precijenio_novi_racun():
    # cuvar: racun 10/2026 po starom cjeniku ispada ~18 % skuplji — upravo
    # greska koju je datumsko verzioniranje cjenika ispravilo
    stari = izracunaj_racun_bijeli(533, 595, 1079, 2, 30, datum='2026-03')['iznos']
    assert stari == pytest.approx(63.39 * 1.18, rel=0.02)


def test_izracunaj_racun_predaja_smanjuje_iznos():
    bez = izracunaj_racun(1000, 0, 30, vt_udio=0.45)
    s_predajom = izracunaj_racun(1000, 500, 30, vt_udio=0.45)
    assert s_predajom == pytest.approx(bez - 500 * HEP_TARIFA['otkup'] * 1.13, abs=0.01)


def test_bijeli_pozitivni_saldo():
    r = izracunaj_racun_bijeli(300, 200, 100, 50, 30)
    assert r['saldo_vt'] == 200
    assert r['saldo_nt'] == 150
    assert r['iznos'] == pytest.approx(round(r['osnovica'] * (1 + HEP_TARIFA['pdv']), 2), abs=0.01)
    assert r['osnovica'] == pytest.approx(r['opskrba'] + r['mreza'], abs=0.01)


def test_bijeli_visak_placa_samo_fiksno():
    # predaja > potrosnja u obje tarife — placaju se samo mjesecne naknade
    r = izracunaj_racun_bijeli(100, 100, 500, 500, 30)
    fiksno = (HEP_TARIFA['opskrbna_mj'] + HEP_TARIFA['mjerna_mj']) * (1 + HEP_TARIFA['pdv'])
    assert r['iznos'] == pytest.approx(fiksno, abs=0.01)
    assert r['saldo_vt'] == -400
    assert r['saldo_nt'] == -400


def test_otkup_viskova():
    o = otkup_viskova_bijeli(100, 100, 500, 500)
    assert o['vt_kwh'] == 400
    assert o['nt_kwh'] == 400
    assert o['vt_eur'] == pytest.approx(400 * HEP_TARIFA['vt_otkup'], abs=0.01)
    assert o['nt_eur'] == pytest.approx(400 * HEP_TARIFA['nt_otkup'], abs=0.01)
    assert o['ukupno_eur'] == pytest.approx(o['vt_eur'] + o['nt_eur'], abs=0.01)


def test_otkup_bez_viska_je_nula():
    o = otkup_viskova_bijeli(500, 500, 100, 100)
    assert o['ukupno_eur'] == 0


def test_get_vt_udio_registri():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE hep_registri (datum TEXT, obis TEXT, value REAL)')
    conn.executemany(
        'INSERT INTO hep_registri (datum, obis, value) VALUES (?,?,?)',
        [
            ('2026-03-31', 'A+_T1', 1000.0), ('2026-03-31', 'A+_T2', 1000.0),
            ('2026-04-30', 'A+_T1', 1450.0), ('2026-04-30', 'A+_T2', 1550.0),
        ],
    )
    po_mjesecu, prosjek = get_vt_udio_registri(conn)
    assert po_mjesecu == {'2026-04': pytest.approx(0.45)}
    assert prosjek == pytest.approx(0.45)


def test_get_vt_udio_registri_preskace_zamjenu_brojila():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE hep_registri (datum TEXT, obis TEXT, value REAL)')
    conn.executemany(
        'INSERT INTO hep_registri (datum, obis, value) VALUES (?,?,?)',
        [
            ('2026-03-31', 'A+_T1', 1000.0), ('2026-03-31', 'A+_T2', 1000.0),
            # novo brojilo — stanje resetirano, delta negativna
            ('2026-04-30', 'A+_T1', 10.0), ('2026-04-30', 'A+_T2', 20.0),
        ],
    )
    po_mjesecu, prosjek = get_vt_udio_registri(conn)
    assert po_mjesecu == {}
    assert prosjek is None
