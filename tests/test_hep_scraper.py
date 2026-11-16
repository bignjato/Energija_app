"""Testovi HEP scraper parsiranja i pohrane (bez mreze)."""

import sqlite3

import hep_scraper


def _conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    hep_scraper.init_db(conn)
    return conn


def test_parse_vrijednost():
    assert hep_scraper.parse_vrijednost('1,64800000') == 1.648
    assert hep_scraper.parse_vrijednost('1648') == 1648.0
    assert hep_scraper.parse_vrijednost(None) == 0.0
    assert hep_scraper.parse_vrijednost('n/a') == 0.0


def test_get_mjeseci_format():
    mjeseci = hep_scraper.get_mjeseci_za_dohvat(40)
    assert len(mjeseci) >= 2
    for m in mjeseci:
        mm, yyyy = m.split('.')
        assert 1 <= int(mm) <= 12
        assert len(yyyy) == 4
    assert len(mjeseci) == len(set(mjeseci))


def test_spremi_krivulje_dijeli_s_4_i_preskace_buducnost():
    conn = _conn()
    podaci = [
        {'Datum': '2026-01-15T10:00:00', 'Value': '4,0'},
        {'Datum': '2099-01-01T00:00:00', 'Value': '8,0'},  # buducnost — preskoci
    ]
    n = hep_scraper.spremi_krivulje(conn, podaci, 'OMM1', je_minus=False)
    assert n == 1
    row = conn.execute('SELECT kwh_plus FROM ocitanja_15min').fetchone()
    assert row['kwh_plus'] == 1.0  # 4,0 / 4


def test_spremi_krivulje_minus_ne_gazi_plus():
    conn = _conn()
    ts = '2026-01-15T10:00:00'
    hep_scraper.spremi_krivulje(conn, [{'Datum': ts, 'Value': '4,0'}], 'OMM1', je_minus=False)
    hep_scraper.spremi_krivulje(conn, [{'Datum': ts, 'Value': '8,0'}], 'OMM1', je_minus=True)
    row = conn.execute('SELECT kwh_plus, kwh_minus FROM ocitanja_15min').fetchone()
    assert row['kwh_plus'] == 1.0
    assert row['kwh_minus'] == 2.0


def test_agregacija_brise_trailing_nule():
    conn = _conn()
    hep_scraper.spremi_krivulje(conn, [
        {'Datum': '2026-01-15T10:00:00', 'Value': '4,0'},
        {'Datum': '2026-01-16T10:00:00', 'Value': '0'},
        {'Datum': '2026-01-17T10:00:00', 'Value': '0'},
    ], 'OMM1', je_minus=False)
    hep_scraper.agregacija(conn, 'OMM1')
    dani = [r['datum'] for r in conn.execute(
        'SELECT datum FROM ocitanja_dnevna ORDER BY datum')]
    assert dani == ['2026-01-15']


# ─── Oznaka kraja intervala (uzorak T pokriva (T-15min, T]) ──────────────────

def _dan_uzoraka(dan, value='4,0'):
    """96 uzoraka jednog dana: dan T00:15 ... (dan+1) T00:00."""
    from datetime import datetime, timedelta
    t0 = datetime.strptime(dan, '%Y-%m-%d')
    return [{'Datum': (t0 + timedelta(minutes=15 * i)).strftime('%Y-%m-%dT%H:%M:%S'),
             'Value': value} for i in range(1, 97)]


def test_agregacija_kraj_intervala_jedan_dan():
    conn = _conn()
    n = hep_scraper.spremi_krivulje(conn, _dan_uzoraka('2026-01-15'), 'OMM1')
    assert n == 96
    n_sat, n_dan = hep_scraper.agregacija(conn, 'OMM1')
    assert (n_sat, n_dan) == (24, 1)

    dnevna = conn.execute('SELECT datum, kwh_plus FROM ocitanja_dnevna').fetchall()
    assert [(r['datum'], r['kwh_plus']) for r in dnevna] == [('2026-01-15', 96.0)]

    satna = conn.execute('SELECT ts, kwh_plus FROM ocitanja_satna ORDER BY ts').fetchall()
    assert [r['ts'] for r in satna] == [f'2026-01-15T{h:02d}:00:00' for h in range(24)]
    assert all(r['kwh_plus'] == 4.0 for r in satna)
    # ukupno ocuvano kroz sve tri razine
    total = conn.execute('SELECT SUM(kwh_plus) FROM ocitanja_15min').fetchone()[0]
    assert total == 96.0 == sum(r['kwh_plus'] for r in satna)


def test_agregacija_ponoc_pripada_prethodnom_danu_i_mjesecu():
    conn = _conn()
    hep_scraper.spremi_krivulje(conn, [
        {'Datum': '2026-01-31T23:45:00', 'Value': '4,0'},
        {'Datum': '2026-02-01T00:00:00', 'Value': '8,0'},   # zadnji interval 31.01.
        {'Datum': '2026-02-01T00:15:00', 'Value': '12,0'},  # prvi interval 01.02.
    ], 'OMM1')
    hep_scraper.agregacija(conn, 'OMM1')
    dnevna = {r['datum']: r['kwh_plus'] for r in conn.execute('SELECT * FROM ocitanja_dnevna')}
    assert dnevna == {'2026-01-31': 3.0, '2026-02-01': 3.0}
    satna = {r['ts']: r['kwh_plus'] for r in conn.execute('SELECT * FROM ocitanja_satna')}
    assert satna == {'2026-01-31T23:00:00': 3.0, '2026-02-01T00:00:00': 3.0}


def test_agregacija_rebuild_uklanja_zastarjele_redove():
    conn = _conn()
    hep_scraper.spremi_krivulje(conn, _dan_uzoraka('2026-01-15'), 'OMM1')
    # ostaci starog grupiranja (po sirovom ts) i red bez ikakvog izvora
    conn.execute("INSERT INTO ocitanja_dnevna (mjerno_mjesto,datum,kwh_plus,kwh_minus) "
                 "VALUES ('OMM1','2026-01-16',1.0,0)")
    conn.execute("INSERT INTO ocitanja_satna (mjerno_mjesto,ts,kwh_plus,kwh_minus) "
                 "VALUES ('OMM1','2026-01-16T00:00:00',1.0,0)")
    conn.execute("INSERT INTO ocitanja_dnevna (mjerno_mjesto,datum,kwh_plus,kwh_minus) "
                 "VALUES ('OMM1','2020-05-05',9.0,0)")
    # drugo mjerno mjesto se ne smije dirati
    conn.execute("INSERT INTO ocitanja_dnevna (mjerno_mjesto,datum,kwh_plus,kwh_minus) "
                 "VALUES ('OMM2','2026-01-16',7.0,0)")
    conn.commit()

    assert hep_scraper.agregacija(conn, 'OMM1') == (24, 1)
    assert [r['datum'] for r in conn.execute(
        "SELECT datum FROM ocitanja_dnevna WHERE mjerno_mjesto='OMM1'")] == ['2026-01-15']
    assert conn.execute("SELECT COUNT(*) FROM ocitanja_satna "
                        "WHERE ts >= '2026-01-16'").fetchone()[0] == 0
    assert conn.execute("SELECT kwh_plus FROM ocitanja_dnevna "
                        "WHERE mjerno_mjesto='OMM2'").fetchone()[0] == 7.0
    # idempotentno
    assert hep_scraper.agregacija(conn, 'OMM1') == (24, 1)


def test_agregacija_trailing_nule_po_kraju_intervala():
    conn = _conn()
    podaci = _dan_uzoraka('2026-01-15') + _dan_uzoraka('2026-01-16', value='0')
    hep_scraper.spremi_krivulje(conn, podaci, 'OMM1')
    assert hep_scraper.agregacija(conn, 'OMM1') == (24, 1)
    # zadnji stvarni uzorak (16.01. T00:00 = kraj 15.01.) ostaje, nule iza njega ne
    assert conn.execute('SELECT MAX(ts), COUNT(*) FROM ocitanja_15min').fetchone()[:] == \
        ('2026-01-16T00:00:00', 96)


def test_agregacija_rollback_pri_gresci():
    conn = _conn()
    hep_scraper.spremi_krivulje(conn, _dan_uzoraka('2026-01-15'), 'OMM1')
    hep_scraper.agregacija(conn, 'OMM1')
    # trigger rusi upis usred rebuilda -> stare tablice moraju ostati netaknute
    conn.execute("CREATE TRIGGER pukni BEFORE INSERT ON ocitanja_dnevna "
                 "BEGIN SELECT RAISE(ABORT, 'test'); END")
    conn.commit()
    try:
        hep_scraper.agregacija(conn, 'OMM1')
        assert False, 'ocekivana iznimka'
    except sqlite3.DatabaseError:
        pass
    assert not conn.in_transaction
    assert conn.execute('SELECT COUNT(*) FROM ocitanja_satna').fetchone()[0] == 24
    assert conn.execute('SELECT COUNT(*) FROM ocitanja_dnevna').fetchone()[0] == 1


# ─── Filter buducih uzoraka ──────────────────────────────────────────────────

def test_spremi_krivulje_filter_buducnosti_na_granici(monkeypatch):
    from datetime import datetime
    conn = _conn()
    monkeypatch.setattr(hep_scraper, '_sada', lambda: datetime(2026, 1, 15, 10, 7, 0))
    n = hep_scraper.spremi_krivulje(conn, [
        {'Datum': '2026-01-15T09:45:00', 'Value': '4,0'},
        {'Datum': '2026-01-15T10:00:00', 'Value': '4,0'},  # zavrsen interval
        {'Datum': '2026-01-15T10:15:00', 'Value': '0'},    # interval jos traje
        {'Datum': '2026-01-16T00:00:00', 'Value': '0'},
    ], 'OMM1')
    assert n == 2
    assert conn.execute('SELECT MAX(ts) FROM ocitanja_15min').fetchone()[0] == '2026-01-15T10:00:00'


def test_spremi_krivulje_sutrasnja_ponoc_je_danasnji_zadnji_interval(monkeypatch):
    from datetime import datetime
    uzorak = [{'Datum': '2026-01-16T00:00:00', 'Value': '4,0'}]

    conn = _conn()
    monkeypatch.setattr(hep_scraper, '_sada', lambda: datetime(2026, 1, 15, 23, 59, 59))
    assert hep_scraper.spremi_krivulje(conn, uzorak, 'OMM1') == 0

    # tocno u ponoc interval je zavrsio; staro pravilo (datum > danas) ga je
    # odbacivalo do kraja dana ako je 'danas' jos 15.01.
    monkeypatch.setattr(hep_scraper, '_sada', lambda: datetime(2026, 1, 16, 0, 0, 0))
    assert hep_scraper.spremi_krivulje(conn, uzorak, 'OMM1') == 1
    hep_scraper.agregacija(conn, 'OMM1')
    assert conn.execute('SELECT datum FROM ocitanja_dnevna').fetchone()[0] == '2026-01-15'


# ─── Lazna HTTP sesija ───────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, json_data=None, text=None, headers=None):
        import json as _json
        self.status_code = status
        self._json = json_data
        if text is None:
            text = '' if json_data is None else _json.dumps(json_data)
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}

    def json(self):
        import json as _json
        return _json.loads(self.text)  # ne-JSON -> ValueError kao requests


class _Sess:
    """Lazna requests sesija: handler(url) -> _Resp ili baca iznimku."""
    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.cookies = {}
        self.pozivi = []

    def post(self, url, **kw):
        self.pozivi.append(url)
        return self.handler(url)


_KUPCI = [{'Naziv': 'Test', 'Oib': '1', 'OmmList': [
    {'Sifra': 'OMM1', 'Adresa': 'Ulica 1', 'Potrosac': True,
     'MjesecOd': '2020-01', 'MjesecDo': '2099-12'}]}]


def _hep(monkeypatch, handler):
    monkeypatch.setattr(hep_scraper, 'BASE_URL', 'https://hep.test/v1.1')
    monkeypatch.setattr(hep_scraper.HEPSession, '__init__', lambda self: None)
    hep = hep_scraper.HEPSession()
    hep.session = _Sess(handler)
    hep.token = None
    hep.logged_in = False
    hep.kupci = []
    hep.greske = []
    hep.greske_registri = []
    hep._detect_pokusan = False
    return hep


def _sync(monkeypatch, tmp_path, handler):
    """Pokreni sync() nad tmp bazom i laznom sesijom; vrati (ok, conn, sesija)."""
    db = tmp_path / 'hep.db'
    monkeypatch.setattr(hep_scraper, 'DB_PATH', db)
    hep = _hep(monkeypatch, handler)
    monkeypatch.setattr(hep_scraper, 'HEPSession', lambda: hep)
    monkeypatch.setattr(hep_scraper, 'get_mjeseci_za_dohvat', lambda dani: ['01.2026'])
    ok = hep_scraper.sync('korisnik', 'tajna-lozinka', 2)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return ok, conn, hep.session


def _zadnji_log(conn):
    return conn.execute('SELECT tip, status, poruka, zapisi FROM sync_log '
                        'ORDER BY id DESC LIMIT 1').fetchone()


def test_login_ne_json_tijelo_je_neuspjela_prijava(monkeypatch, caplog):
    hep = _hep(monkeypatch, lambda url: _Resp(200, text='<html>Odrzavanje</html>',
                                             headers={'Content-Type': 'text/html'}))
    with caplog.at_level('DEBUG'):
        assert hep.login('korisnik', 'tajna-lozinka') is False
    assert hep.logged_in is False
    assert 'tajna-lozinka' not in caplog.text


def test_login_ne_json_upisuje_error_u_sync_log(monkeypatch, tmp_path):
    ok, conn, _ = _sync(monkeypatch, tmp_path, lambda url: _Resp(200, text='nije json'))
    assert ok is False
    log = _zadnji_log(conn)
    assert (log['tip'], log['status']) == ('login', 'ERROR')


# ─── Agregacija neovisna o portalu (jednokratno pregrupiranje povijesti) ─────

def _stara_povijest(monkeypatch, tmp_path, mid):
    """Baza kakvu ostavlja stari kod: 15-min uzorci + agregati po sirovom ts."""
    db = tmp_path / 'hep.db'
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    hep_scraper.init_db(conn)
    hep_scraper.spremi_krivulje(conn, _dan_uzoraka('2026-01-15'), mid)
    conn.execute("INSERT INTO ocitanja_dnevna (mjerno_mjesto,datum,kwh_plus,kwh_minus) "
                 "VALUES (?,'2026-01-15',95.0,0)", (mid,))
    conn.execute("INSERT INTO ocitanja_dnevna (mjerno_mjesto,datum,kwh_plus,kwh_minus) "
                 "VALUES (?,'2026-01-16',1.0,0)", (mid,))
    conn.execute("INSERT INTO ocitanja_satna (mjerno_mjesto,ts,kwh_plus,kwh_minus) "
                 "VALUES (?,'2026-01-16T00:00:00',1.0,0)", (mid,))
    conn.commit()
    conn.close()


def _provjeri_pregrupirano(conn, mid):
    dnevna = {r['datum']: r['kwh_plus'] for r in conn.execute(
        'SELECT datum, kwh_plus FROM ocitanja_dnevna WHERE mjerno_mjesto=?', (mid,))}
    assert dnevna == {'2026-01-15': 96.0}
    satna = [r['ts'] for r in conn.execute(
        'SELECT ts FROM ocitanja_satna WHERE mjerno_mjesto=? ORDER BY ts', (mid,))]
    assert len(satna) == 24 and satna[-1] == '2026-01-15T23:00:00'


def test_sync_neuspjela_prijava_ipak_pregrupira_povijest(monkeypatch, tmp_path):
    _stara_povijest(monkeypatch, tmp_path, 'OMM1')
    ok, conn, _ = _sync(monkeypatch, tmp_path, lambda url: _Resp(200, text='nije json'))
    assert ok is False
    assert (_zadnji_log(conn)['tip'], _zadnji_log(conn)['status']) == ('login', 'ERROR')
    _provjeri_pregrupirano(conn, 'OMM1')


def test_sync_bez_mjernih_mjesta_ipak_pregrupira_povijest(monkeypatch, tmp_path):
    _stara_povijest(monkeypatch, tmp_path, 'OMM1')
    ok, conn, _ = _sync(monkeypatch, tmp_path, lambda url: _Resp(200, []))
    assert ok is False
    _provjeri_pregrupirano(conn, 'OMM1')


def test_sync_pregrupira_i_mjerno_mjesto_kojeg_nema_u_prijavi(monkeypatch, tmp_path):
    _stara_povijest(monkeypatch, tmp_path, 'STARO')

    def handler(url):
        if url.endswith('/api/user/login'):
            return _Resp(200, _KUPCI)  # vraca samo OMM1
        if '/krivulja/' in url:
            return _Resp(200, [])
        return _Resp(204)

    ok, conn, _ = _sync(monkeypatch, tmp_path, handler)
    assert ok is True
    _provjeri_pregrupirano(conn, 'STARO')


def test_agregiraj_sve_greska_jednog_ne_prekida_ostale(monkeypatch):
    conn = _conn()
    hep_scraper.spremi_krivulje(conn, _dan_uzoraka('2026-01-15'), 'OMM1')
    hep_scraper.spremi_krivulje(conn, _dan_uzoraka('2026-01-15'), 'OMM2')
    prava = hep_scraper.agregacija

    def krhka(c, mid):
        if mid == 'OMM1':
            raise sqlite3.OperationalError('database is locked')
        return prava(c, mid)

    monkeypatch.setattr(hep_scraper, 'agregacija', krhka)
    assert hep_scraper.agregiraj_sve(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM ocitanja_dnevna "
                        "WHERE mjerno_mjesto='OMM2'").fetchone()[0] == 1


def test_sync_krivulja_500_je_error(monkeypatch, tmp_path, caplog):
    def handler(url):
        if url.endswith('/api/user/login'):
            return _Resp(200, _KUPCI)
        if '/krivulja/' in url and url.endswith('/smjer/P'):
            return _Resp(500, text='Internal Server Error')
        if '/krivulja/' in url:
            return _Resp(200, [{'Datum': '2026-01-15T10:00:00', 'Value': '4,0'}])
        return _Resp(204)

    with caplog.at_level('WARNING'):
        ok, conn, _ = _sync(monkeypatch, tmp_path, handler)
    assert ok is False
    log = _zadnji_log(conn)
    assert (log['tip'], log['status']) == ('sync', 'ERROR')
    assert 'HTTP 500' in log['poruka']
    assert 'HTTP 500' in caplog.text
    assert 'tajna-lozinka' not in caplog.text and 'tajna-lozinka' not in log['poruka']
    # ono sto je uspjesno stiglo (smjer R) je ipak spremljeno
    assert conn.execute('SELECT kwh_minus FROM ocitanja_15min').fetchone()[0] == 1.0


def test_sync_iznimka_pri_dohvatu_je_error(monkeypatch, tmp_path):
    def handler(url):
        if url.endswith('/api/user/login'):
            return _Resp(200, _KUPCI)
        if '/krivulja/' in url:
            raise ConnectionError('mreza')
        return _Resp(204)

    ok, conn, _ = _sync(monkeypatch, tmp_path, handler)
    assert ok is False
    log = _zadnji_log(conn)
    assert log['status'] == 'ERROR' and 'ConnectionError' in log['poruka']


def test_sync_prazan_mjesec_200_je_ok(monkeypatch, tmp_path):
    def handler(url):
        if url.endswith('/api/user/login'):
            return _Resp(200, _KUPCI)
        if '/krivulja/' in url:
            return _Resp(200, [])
        return _Resp(204)

    ok, conn, _ = _sync(monkeypatch, tmp_path, handler)
    assert ok is True
    log = _zadnji_log(conn)
    assert (log['tip'], log['status'], log['zapisi']) == ('sync', 'OK', 0)


def test_sync_bez_mjernih_mjesta_je_error(monkeypatch, tmp_path):
    ok, conn, _ = _sync(monkeypatch, tmp_path, lambda url: _Resp(200, []))
    assert ok is False
    log = _zadnji_log(conn)
    assert (log['tip'], log['status']) == ('sync', 'ERROR')


def test_registri_greska_je_warning_ali_sync_ok(monkeypatch, tmp_path, caplog):
    def handler(url):
        if url.endswith('/api/user/login'):
            return _Resp(200, _KUPCI)
        if '/krivulja/' in url:
            return _Resp(200, [{'Datum': '2026-01-15T10:00:00', 'Value': '4,0'}])
        if url.endswith('/12.2025/registri'):
            return _Resp(503, text='Service Unavailable')
        return _Resp(204)  # tekuci mjesec nije zakljucen — tiho

    with caplog.at_level('WARNING'):
        ok, conn, _ = _sync(monkeypatch, tmp_path, handler)
    assert ok is True
    assert 'registri: HTTP 503' in caplog.text
    assert '01.2026 registri' not in caplog.text
    log = _zadnji_log(conn)
    assert log['status'] == 'OK' and 'registri: 1' in log['poruka']


def test_krivulje_404_detect_base_url_i_retry(monkeypatch):
    detekcije = []

    def handler(url):
        if url.startswith('https://hep.test/v1.1/'):
            return _Resp(404, text='Not Found')
        return _Resp(200, [{'Datum': '2026-01-15T10:00:00', 'Value': '4,0'}])

    hep = _hep(monkeypatch, handler)
    monkeypatch.setattr(hep_scraper, 'detect_base_url',
                        lambda: detekcije.append(1) or 'https://hep.test/v2')

    data = hep.get_krivulje_mjesec('OMM1', '01.2026', smjer='P')
    assert len(data) == 1 and hep.greske == []
    assert hep_scraper.BASE_URL == 'https://hep.test/v2'
    assert hep.session.pozivi == [
        'https://hep.test/v1.1/api/data/omm/OMM1/krivulja/mjesec/01.2026/smjer/P',
        'https://hep.test/v2/api/data/omm/OMM1/krivulja/mjesec/01.2026/smjer/P',
    ]
    # iduci zahtjevi idu ravno na novu bazu, bez ponovne detekcije
    assert hep.get_registri_mjesec('OMM1', '01.2026') != []
    assert len(detekcije) == 1


def test_krivulje_404_bez_nove_baze_je_greska(monkeypatch):
    detekcije = []
    hep = _hep(monkeypatch, lambda url: _Resp(404, text='Not Found'))
    monkeypatch.setattr(hep_scraper, 'detect_base_url',
                        lambda: detekcije.append(1) or 'https://hep.test/v1.1')

    assert hep.get_krivulje_mjesec('OMM1', '01.2026', smjer='P') == []
    assert hep.get_krivulje_mjesec('OMM1', '01.2026', smjer='R') == []
    assert len(hep.greske) == 2 and 'HTTP 404' in hep.greske[0]
    assert len(detekcije) == 1           # detekcija samo jednom po sesiji
    assert len(hep.session.pozivi) == 2  # bez retry-a na istu bazu
