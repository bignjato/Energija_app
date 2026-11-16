"""Testovi za HEP PDF parser (parse_hep_bill nad sintetickim tekstom racuna).

Tekst je slozen prema stvarnom HEPI bijeli (E-K-N-BIJ1) layoutu koji
pdfplumber izvuce iz PDF-a — ako HEP promijeni format, ovi testovi pucaju
prvi i pokazu koje polje je prestalo raditi.
"""

import pytest

from hepapp.bill_parser import _to_float, missing_key_fields, parse_hep_bill

SAMPLE = """HEP ELEKTRA d.o.o.
Šifra kupca: 1234567
Broj računa: 1234567-2026-04
Model: HEPI bijeli
tarifni model: E-K-N-BIJ1
Datum računa: 05.05.2026
Razdoblje: 01.04.2026 - 30.04.2026
viša tarifa - potrošnja 549
niža tarifa - potrošnja 622
viša tarifa - proizvodnja 320
niža tarifa - proizvodnja 95
opskrbna naknada po 0,982 EUR/mjesec
naknada za mjernu uslugu (br.mjeseci) po 1,983 EUR
Ukupni iznos računa za 4/2026
Ukupan iznos za opskrbu 49,18
Ukupni iznos za korištenje mreže i usluga 19,80 € 79,10 €
PDV 13% (osnovica: 68,98) 8,97 €
Kamata - PDV nije obračunat 1,15 €
Vaš dug 1.054,72 €
Platite do: 20.05.2026
"""


def test_to_float_hr_formati():
    assert _to_float('1.054,72') == 1054.72
    assert _to_float('8,97') == 8.97
    assert _to_float('549') == 549.0
    assert _to_float('79,10 €') == 79.10
    assert _to_float(None) is None
    assert _to_float('n/a') is None


@pytest.fixture(scope='module')
def parsed():
    return parse_hep_bill(SAMPLE)


def test_period_i_razdoblje(parsed):
    assert parsed['period'] == '04/2026'
    assert parsed['od'] == '2026-04-01'
    assert parsed['do'] == '2026-04-30'


def test_iznos_iz_dvostupcanog_layouta(parsed):
    # dva iznosa na liniji — drugi (desni box) je ukupni iznos racuna
    assert parsed['iznos'] == 79.10


def test_potrosnja_vt_nt(parsed):
    assert parsed['kwh_vt'] == 549
    assert parsed['kwh_nt'] == 622
    assert parsed['kwh_plus'] == 1171


def test_proizvodnja(parsed):
    assert parsed['kwh_minus'] == 415  # 320 VT + 95 NT


def test_komponente(parsed):
    assert parsed['opskrba'] == 49.18
    assert parsed['mreza'] == 19.80
    assert parsed['pdv'] == 8.97
    assert parsed['kamata'] == 1.15


def test_dug_i_naknade(parsed):
    assert parsed['dug'] == 1054.72
    assert parsed['pretplata'] == 0.982
    assert parsed['mjerna_mjernina'] == 1.983


def test_metapodaci(parsed):
    assert parsed['datum_racuna'] == '2026-05-05'
    assert parsed['datum_dospijeca'] == '2026-05-20'
    assert parsed['model_tarife'] == 'HEPI bijeli — E-K-N-BIJ1'
    assert parsed['sifra_kupca'] == '1234567'
    assert parsed['broj_racuna'] == '1234567-2026-04'


def test_prazan_tekst_ne_puca():
    res = parse_hep_bill('')
    assert res['iznos'] is None
    assert res['period'] is None


def test_missing_key_fields_potpun_racun(parsed):
    assert missing_key_fields(parsed) == []


def test_missing_key_fields_prazan():
    res = parse_hep_bill('')
    nedostaje = missing_key_fields(res)
    # prazan tekst => sva ključna polja nedostaju
    assert 'ukupni iznos' in nedostaje
    assert 'ukupna potrošnja' in nedostaje
    assert 'PDV' in nedostaje
    assert len(nedostaje) == 6


def test_missing_key_fields_djelomicno():
    # samo iznos i period prepoznati, mreža/pdv/opskrba/potrošnja fale
    txt = ("Ukupni iznos računa za 4/2026\n"
           "Ukupni iznos za korištenje mreže 19,80 € 79,10 €\n")
    res = parse_hep_bill(txt)
    nedostaje = missing_key_fields(res)
    assert 'ukupni iznos' not in nedostaje      # 79,10 prepoznat
    assert 'razdoblje računa' not in nedostaje   # 4/2026 prepoznat
    assert 'PDV' in nedostaje
    assert 'ukupna potrošnja' in nedostaje


# ---------------------------------------------------------------------------
# Jedinicne cijene s racuna (res['cijene']) + otkup
#
# Tekst je stvarni pdfplumber izlaz racuna 10/2026 (HEPI bijeli); osobni
# podaci (ime, adresa, OIB, sifra kupca, IBAN, broj racuna, OMM) zamijenjeni
# su placeholderima. Lijevi (opskrba) i desni (mreza) stupac su u istoj liniji.
# ---------------------------------------------------------------------------

RACUN_10_2026 = """HEP - Opskrba d.o.o. OIB: 00000000000 Datum računa: 31.10.2026.
ZAGREB, Ulica grada Vukovara 37 Račun izdan u Zagrebu: 10.11.2026.
IBAN: HR0000000000000000000 Banka d.d. Platite do: 25.11.2026. (datum dospijeća)
Kupac: IME PREZIME IME PREZIME
Šifra kupca: 12345678
OIB: 11111111111
ULICA 1
10000 MJESTO
Ukupni iznos računa za 10/2026
Ukupni iznos za opskrbu 37,11 €
Ukupni iznos za korištenje mreže i usluga 18,99 € 63,39 €
PDV 13% (osnovica: 56,10) 7,29 €
Broj računa: 0012345678-261020-0
Model: HEPI bijeli
Molimo podmirite dug! Vaš dug
Na dan izdavanja računa nepodmireni iznos je 150,83 eura. Molimo podmirite dugovanje odmah
kako biste neometano nastavili koristiti našu uslugu. 150,83 €
• OTKUP: U listopadu smo od Vas otkupili 546 kWh električne energije - za 42,45 eura.
Podaci na poleđini su sastavni dio računa Hvala Vam što ste s nama,
IME PREZIME - MJESTO, ULICA 1
Broj obračunskog mjesta: 0000000000
OBRAČUN OPSKRBE Model: HEPI bijeli OBRAČUN MREŽE tarifni model: E-K-N-BIJ1
01.10.2026 - 31.10.2026 Količina € 01.10.2026 - 31.10.2026 Količina €
viša tarifa po 0,077751 EUR/kWh -546 0,00 distribucija - viša tarifa po 0,044446 EUR/kWh -546 0,00
niža tarifa po 0,047688 EUR/kWh 593 28,28 distribucija - niža tarifa po 0,020514 EUR/kWh 593 12,16
viša tarifa - potrošnja 533 naknada za mjernu uslugu (br.mjeseci) po 1,983 EUR 1 1,98
niža tarifa - potrošnja 595 prijenos - viša tarifa po 0,021256 EUR/kWh -546 0,00
viša tarifa - proizvodnja 1079 prijenos - niža tarifa po 0,008175 EUR/kWh 593 4,85
niža tarifa - proizvodnja 2 Ukupan iznos za korištenje mreže 18,99
solidarna naknada po 0,003982 EUR/kWh 593 2,36
obnovljivi izvori po 0,013239 EUR/kWh 593 7,85
opskrbna naknada po 0,982 EUR/mjesec 1 0,98
popust na solidarnu naknadu 1 -2,36
Ukupan iznos za opskrbu 37,11
UPLATE OČITANJA U RAZDOBLJU: 30.09.2026. - 31.10.2026. Brojilo: proizvodnja
Datum uplate € Datum Kanal Viša tarifa Viša tarifa - Niža tarifa Niža tarifa -
31.10.2026 42,45 31.10.2026 Daljinsko 38040,63 69026,91 36332,86 1766,04
Ukupno uplate 42,45 01.10.2026 Daljinsko 37507,40 67947,92 35738,25 1764,45
"""

CIJENE_10_2026 = {
    'vt_opskrba': 0.077751, 'nt_opskrba': 0.047688,
    'vt_distrib': 0.044446, 'nt_distrib': 0.020514,
    'vt_prijenos': 0.021256, 'nt_prijenos': 0.008175,
    'solidarna': 0.003982, 'oie': 0.013239,
    'opskrbna_mj': 0.982, 'mjerna_mj': 1.983,
}


@pytest.fixture(scope='module')
def racun():
    return parse_hep_bill(RACUN_10_2026)


def test_stvarni_layout_osnovna_polja(racun):
    assert racun['period'] == '10/2026'
    assert racun['iznos'] == 63.39
    assert racun['opskrba'] == 37.11
    assert racun['mreza'] == 18.99
    assert racun['pdv'] == 7.29
    assert racun['kwh_vt'] == 533
    assert racun['kwh_nt'] == 595
    assert racun['kwh_minus'] == 1081
    assert racun['dug'] == 150.83
    assert missing_key_fields(racun) == []


def test_cijene_s_racuna(racun):
    c = racun['cijene']
    for k, v in CIJENE_10_2026.items():
        assert c[k] == v, k
    # "viša tarifa po" opskrbe se ne smije zamijeniti s distribucijom/prijenosom
    assert c['vt_opskrba'] != c['vt_distrib'] != c['vt_prijenos']


def test_otkup_s_racuna(racun):
    assert racun['otkup_kwh'] == 546
    assert racun['otkup_eur'] == 42.45
    # cisti VT visak (VT saldo -546, NT saldo 593) -> otkupna cijena se da izvesti
    # 546 x 0,077751 = 42,45 -> tocna VT cijena opskrbe, a ne 42,45/546 = 0,077747
    assert racun['cijene']['vt_otkup'] == racun['cijene']['vt_opskrba'] == 0.077751
    assert racun['cijene']['otkup'] == racun['cijene']['vt_otkup']
    assert 'nt_otkup' not in racun['cijene']      # NT otkup se ne nagadja
    assert set(racun['cijene']) == set(CIJENE_10_2026) | {'vt_otkup', 'otkup'}


def test_cijene_redoslijed_stupaca_nije_bitan():
    # mreza ispred opskrbe i bez razmaka oko crtice
    txt = ("distribucija-viša tarifa po 0,044446 EUR/kWh 10 0,44\n"
           "prijenos – niža tarifa po 0,008175 EUR/kWh 10 0,08\n"
           "niža tarifa po 0,047688 EUR/kWh 10 0,48\n"
           "viša tarifa po 0,077751 EUR/kWh 10 0,78\n")
    assert parse_hep_bill(txt)['cijene'] == {
        'vt_distrib': 0.044446, 'nt_prijenos': 0.008175,
        'nt_opskrba': 0.047688, 'vt_opskrba': 0.077751,
    }


def test_cijene_linija_koje_nema_nema_ni_kljuca(parsed):
    # SAMPLE ima samo dvije mjesecne naknade
    assert parsed['cijene'] == {'opskrbna_mj': 0.982, 'mjerna_mj': 1.983}
    assert parsed['otkup_kwh'] is None
    assert parse_hep_bill('')['cijene'] == {}


@pytest.mark.parametrize('vt_saldo,nt_saldo,otkup', [
    ('-546', '-10', '546'),    # visak i u NT -> mjesavina cijena, ne izvodi se
    ('120', '593', '546'),     # nema VT viska
    ('-546', '593', '300'),    # otkupljena kolicina ne odgovara VT visku
])
def test_otkupna_cijena_se_ne_izvodi_kad_nije_cisti_vt_visak(vt_saldo, nt_saldo, otkup):
    txt = (f"viša tarifa po 0,077751 EUR/kWh {vt_saldo} 0,00\n"
           f"niža tarifa po 0,047688 EUR/kWh {nt_saldo} 0,00\n"
           f"• OTKUP: U listopadu smo od Vas otkupili {otkup} kWh električne energije - za 42,45 eura.\n")
    res = parse_hep_bill(txt)
    assert res['otkup_kwh'] == float(otkup)
    assert res['otkup_eur'] == 42.45
    assert 'vt_otkup' not in res['cijene']
    assert 'otkup' not in res['cijene']
    assert 'nt_otkup' not in res['cijene']


def test_otkup_tisucice():
    txt = ("viša tarifa po 0,077751 EUR/kWh -2445 0,00\n"
           "niža tarifa po 0,047688 EUR/kWh 355 16,93\n"
           "OTKUP: U lipnju smo od Vas otkupili 2.445 kWh električne energije - za 190,10 eura.\n")
    res = parse_hep_bill(txt)
    assert res['otkup_kwh'] == 2445
    assert res['otkup_eur'] == 190.10
    assert res['cijene']['vt_otkup'] == res['cijene']['otkup'] == 0.077751


def _racun_s_otkupom(kwh, eur, vt_linija="viša tarifa po 0,077751 EUR/kWh"):
    return parse_hep_bill(
        f"{vt_linija} -{kwh} 0,00\n"
        "niža tarifa po 0,047688 EUR/kWh 355 16,93\n"
        f"OTKUP: U studenom smo od Vas otkupili {kwh} kWh električne energije - za {eur} eura.\n")


def test_otkupna_cijena_mali_visak_nije_eur_kroz_kwh():
    # 3 kWh za 0,23 EUR: 0,23/3 = 0,076667 (-1,4 %), a iznos se slaze s VT cijenom
    res = _racun_s_otkupom(3, '0,23')
    assert res['otkup_kwh'] == 3 and res['otkup_eur'] == 0.23
    assert res['cijene']['vt_otkup'] == res['cijene']['otkup'] == 0.077751


def test_otkupna_cijena_mali_visak_po_drugoj_cijeni_izostaje():
    # iznos se ne slaze s VT cijenom, a 3 kWh je premalo za dijeljenje -> bez kljuca
    res = _racun_s_otkupom(3, '0,19')
    assert res['otkup_kwh'] == 3 and res['otkup_eur'] == 0.19
    assert 'vt_otkup' not in res['cijene']
    assert 'otkup' not in res['cijene']
    # isto i kad VT cijene opskrbe na racunu nema
    res = _racun_s_otkupom(3, '0,23', vt_linija="viša tarifa - saldo")
    assert 'vt_otkup' not in res['cijene']


def test_otkupna_cijena_veliki_visak_po_drugoj_cijeni_se_dijeli():
    # otkup nije po VT cijeni opskrbe, ali je kolicina dovoljna (cent ne smeta)
    res = _racun_s_otkupom(500, '32,19')
    assert res['cijene']['vt_opskrba'] == 0.077751
    assert res['cijene']['vt_otkup'] == res['cijene']['otkup'] == 0.06438


# ---------------------------------------------------------------------------
# Ukupni iznos, strategija 2 (suma komponenti)
# ---------------------------------------------------------------------------

def test_iznos_strategija_2_suma_komponenti():
    # nema linije s dva iznosa (desni box) -> opskrba + mreza + PDV + kamata.
    # Prije se suma racunala PRIJE parsiranja komponenti pa je iznos bio samo kamata.
    txt = ("Ukupni iznos računa za 10/2026\n"
           "Ukupni iznos za opskrbu 37,11 €\n"
           "Ukupni iznos za korištenje mreže i usluga 18,99 €\n"
           "PDV 13% (osnovica: 56,10) 7,29 €\n"
           "Kamata - PDV nije obračunat 1,15 €\n")
    res = parse_hep_bill(txt)
    assert res['iznos'] == pytest.approx(37.11 + 18.99 + 7.29 + 1.15)
    bez_kamate = parse_hep_bill(txt.rsplit('Kamata', 1)[0])
    assert bez_kamate['iznos'] == 63.39


def test_iznos_sama_kamata_nije_iznos_racuna():
    res = parse_hep_bill("Ukupni iznos računa za 10/2026\nKamata - PDV nije obračunat 1,15 €\n")
    assert res['kamata'] == 1.15
    assert res['iznos'] is None
    assert 'ukupni iznos' in missing_key_fields(res)


# ---------------------------------------------------------------------------
# Uvoz racuna: cijene prezive upload-pdf -> POST /api/racuni bez izmjene JS-a
# (frontend u POST-u salje samo polja forme, bez cijena)
# ---------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch, tmp_path):
    import io
    import sqlite3

    from flask import Flask

    import hepapp.db as db
    from hepapp.blueprints import racuni as racuni_bp
    from hepapp.tariff import invalidate_tarifa_cache

    path = str(tmp_path / 'hep.db')
    monkeypatch.setattr(db, 'DB_PATH', path)
    monkeypatch.setenv('INITIAL_ADMIN_PASSWORD', 'test-lozinka')
    db.init_db()
    db.init_db()      # idempotentno
    invalidate_tarifa_cache()

    # samo racuni blueprint — bez login/CSRF sloja, sesija se postavi rucno
    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(racuni_bp.bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s['logged_in'] = True
        s['uloga'] = 'admin'

    def upload(text, filename='racun.pdf'):
        monkeypatch.setattr(racuni_bp, 'extract_text', lambda pdf_bytes: text)
        return c.post('/api/racuni/upload-pdf',
                      data={'file': (io.BytesIO(b'%PDF-1.4'), filename)},
                      content_type='multipart/form-data').get_json()

    def red(period):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        r = conn.execute('SELECT * FROM racuni WHERE period=?', (period,)).fetchone()
        conn.close()
        return dict(r) if r else None

    c.upload, c.red, c.path = upload, red, path
    yield c
    invalidate_tarifa_cache()


def _post_kao_frontend(d):
    """Tijelo POST-a kakvo slaze dashboard.js addRacun() — bez cijena."""
    mm, yyyy = d['period'].split('/')
    return {
        'period': f'{yyyy}-{mm}', 'iznos': d['iznos'],
        'kwh_vt': d['kwh_vt'], 'kwh_nt': d['kwh_nt'],
        'kwh_plus': (d['kwh_vt'] or 0) + (d['kwh_nt'] or 0), 'kwh_minus': d['kwh_minus'],
        'opskrba': d['opskrba'], 'mreza': d['mreza'], 'pdv': d['pdv'],
        'dug': d['dug'], 'pretplata': d['pretplata'],
        'mjerna_mjernina': d['mjerna_mjernina'], 'kamata': d['kamata'],
        'datum_racuna': d['datum_racuna'], 'datum_dospijeca': d['datum_dospijeca'],
        'model_tarife': d['model_tarife'],
        'sifra_kupca': d['sifra_kupca'], 'broj_racuna': d['broj_racuna'],
    }


def test_uvoz_pdf_cijene_prezive_spremanje(api):
    import json

    from hepapp.tariff import get_tarifa, get_tarifa_izvor

    d = api.upload(RACUN_10_2026)
    assert d['ok'] is True
    assert d['cijene']['vt_opskrba'] == 0.077751
    assert api.red('2026-10') is None                 # upload sam ne sprema racun
    assert get_tarifa_izvor('2026-10') == 'ugrađeni cjenik od 2026-04-01'

    assert api.post('/api/racuni', json=_post_kao_frontend(d)).get_json() == {'ok': True}
    r = api.red('2026-10')
    assert r['iznos'] == 63.39
    cijene = json.loads(r['cijene_json'])
    assert cijene == {**CIJENE_10_2026, 'vt_otkup': 0.077751, 'otkup': 0.077751}

    # spremanje je ponistilo kes -> cjenik odmah prati racun
    assert get_tarifa_izvor('2026-10') == 'račun 2026-10'
    assert get_tarifa_izvor('2026-12') == 'račun 2026-10'
    assert get_tarifa('2026-11')['vt_otkup'] == 0.077751

    # GET vraca i cijene_json
    lista = api.get('/api/racuni').get_json()
    assert json.loads(lista[0]['cijene_json'])['oie'] == 0.013239

    # rucni ispravak iznosa (bez PDF-a) ne smije obrisati cijene
    api.post('/api/racuni', json={'period': '2026-10', 'iznos': 64.0})
    r = api.red('2026-10')
    assert r['iznos'] == 64.0
    assert json.loads(r['cijene_json']) == cijene

    # brisanje racuna -> natrag na ugradjeni cjenik
    assert api.delete('/api/racuni?period=2026-10').get_json() == {'ok': True}
    assert get_tarifa_izvor('2026-10') == 'ugrađeni cjenik od 2026-04-01'
    api.post('/api/racuni', json={'period': '2026-10', 'iznos': 64.0})
    assert api.red('2026-10')['cijene_json'] is None


def test_rucni_unos_bez_pdf_a(api):
    from hepapp.tariff import get_tarifa_izvor

    assert api.post('/api/racuni', json={
        'period': '2026-09', 'iznos': 55.5, 'kwh_vt': 100, 'kwh_nt': 200,
    }).get_json() == {'ok': True}
    r = api.red('2026-09')
    assert r['iznos'] == 55.5
    assert r['cijene_json'] is None
    assert get_tarifa_izvor('2026-09') == 'ugrađeni cjenik od 2026-04-01'


def test_cijene_izravno_u_postu(api):
    import json

    api.post('/api/racuni', json={
        'period': '2026-08', 'iznos': 40.0,
        'cijene': {'vt_opskrba': 0.08, 'smece': 'x', 'nt_opskrba': None},
    })
    assert json.loads(api.red('2026-08')['cijene_json']) == {'vt_opskrba': 0.08}


def test_pdf_drugog_racuna_ne_daje_cijene(api):
    # PDF parsiran za 10/2026, a sprema se racun s drugim brojem za isti period
    d = api.upload(RACUN_10_2026)
    tijelo = _post_kao_frontend(d)
    tijelo['broj_racuna'] = '9999999999-000000-0'
    api.post('/api/racuni', json=tijelo)
    assert api.red('2026-10')['cijene_json'] is None
    # ... ni racun za drugi mjesec
    tijelo = _post_kao_frontend(d)
    tijelo['period'] = '2026-09'
    api.post('/api/racuni', json=tijelo)
    assert api.red('2026-09')['cijene_json'] is None


def test_spremanje_radi_i_na_staroj_shemi(api):
    # init_db/migracija nije prosla (nema cijene_json ni racuni_cijene_pdf)
    import sqlite3
    conn = sqlite3.connect(api.path)
    conn.execute('ALTER TABLE racuni DROP COLUMN cijene_json')
    conn.execute('DROP TABLE racuni_cijene_pdf')
    conn.commit()
    conn.close()
    d = api.upload(RACUN_10_2026)
    assert d['ok'] is True
    assert api.post('/api/racuni', json=_post_kao_frontend(d)).get_json() == {'ok': True}
    assert api.red('2026-10')['iznos'] == 63.39


def test_spremanje_radi_kad_nedostaje_samo_kolona(api):
    # CREATE racuni_cijene_pdf je prosao, ALTER za cijene_json nije (zakljucana baza)
    import sqlite3

    import hepapp.db as db
    conn = sqlite3.connect(api.path)
    conn.execute('ALTER TABLE racuni DROP COLUMN cijene_json')
    conn.commit()
    conn.close()
    d = api.upload(RACUN_10_2026)
    assert d['ok'] is True
    r = api.post('/api/racuni', json=_post_kao_frontend(d))
    assert r.status_code == 200 and r.get_json() == {'ok': True}
    assert api.red('2026-10')['iznos'] == 63.39
    # nakon sto migracija prodje, ponovno spremanje pokupi zapamcene cijene s PDF-a
    conn = sqlite3.connect(api.path)
    conn.execute('ALTER TABLE racuni ADD COLUMN cijene_json TEXT')
    conn.commit()
    conn.close()
    assert api.post('/api/racuni', json=_post_kao_frontend(d)).get_json() == {'ok': True}
    assert '"vt_opskrba": 0.077751' in api.red('2026-10')['cijene_json']


def test_init_db_migracija_i_indeksi(monkeypatch, tmp_path):
    import sqlite3

    import hepapp.db as db

    path = str(tmp_path / 'stara.db')
    # postojeca instalacija na shemi v1 + tablice koje kreiraju scraperi (bez sma_live)
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE racuni (id INTEGER PRIMARY KEY AUTOINCREMENT, period TEXT NOT NULL UNIQUE,
                             iznos REAL NOT NULL, broj_racuna TEXT);
        INSERT INTO racuni (period, iznos) VALUES ('2026-09', 50.0);
        CREATE TABLE ocitanja_15min (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL, kwh_minus REAL);
        CREATE TABLE ocitanja_satna (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL, kwh_minus REAL);
        CREATE TABLE ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT, kwh_plus REAL, kwh_minus REAL);
    ''')
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, 'DB_PATH', path)
    monkeypatch.setenv('INITIAL_ADMIN_PASSWORD', 'test-lozinka')
    db.init_db()      # sma_live ne postoji -> ne smije pasti
    db.init_db()

    conn = sqlite3.connect(path)
    kolone = {r[1] for r in conn.execute('PRAGMA table_info(racuni)')}
    assert 'cijene_json' in kolone
    assert conn.execute('SELECT iznos, cijene_json FROM racuni').fetchone() == (50.0, None)
    indeksi = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {'idx_15min_ts_only', 'idx_satna_ts_only', 'idx_dnevna_datum_only'} <= indeksi
    assert 'idx_sma_live_ts' not in indeksi
    assert conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] == db.SCHEMA_VERSION
    # tablica se pojavi kasnije (sma scraper) -> indeks nastane na iducem startu
    conn.execute('CREATE TABLE sma_live (ts TEXT, pv_w REAL)')
    conn.commit()
    conn.close()
    db.init_db()
    conn = sqlite3.connect(path)
    indeksi = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert 'idx_sma_live_ts' in indeksi
