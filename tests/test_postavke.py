"""Testovi za /api/postavke*: korisnici (uloge, zadnji admin), backup, poruka o .env."""

import os
import sqlite3
import tempfile

import pytest

ORIGIN = {'Origin': 'http://localhost'}


@pytest.fixture
def app(monkeypatch, tmp_path):
    fd, path = tempfile.mkstemp(suffix='.db', dir=str(tmp_path))
    os.close(fd)
    monkeypatch.setenv('SECRET_KEY', '0123456789abcdef0123456789abcdef')
    monkeypatch.setenv('INITIAL_ADMIN_PASSWORD', 'admin-lozinka')

    import hepapp.db as db
    monkeypatch.setattr(db, 'DB_PATH', path)
    from app import app as flask_app
    import hepapp.auth as auth
    import hepapp.core as core
    import hepapp.blueprints.postavke as postavke
    monkeypatch.setattr(postavke, 'DB_PATH', path)
    # .env se pise u tmp, ne u repo
    monkeypatch.setattr(postavke, 'ENV_PATH', str(tmp_path / '.env'))
    db.init_db()
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('_setup_complete','1')")
    conn.execute("INSERT INTO korisnici (username, password_hash, uloga) VALUES (?,?,?)",
                 ('gost', core.hash_password('gost-lozinka'), 'viewer'))
    conn.commit(); conn.close()

    monkeypatch.setattr(auth, 'LOGIN_PASSWORD', '')
    if core.limiter is not None:
        monkeypatch.setattr(core.limiter, 'enabled', False)
    flask_app.config.update(TESTING=True)
    flask_app.db_path = path
    return flask_app


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post('/login', data={'username': 'admin', 'password': 'admin-lozinka'}).status_code == 302
    return c


def _rows(app, sql, args=()):
    conn = sqlite3.connect(app.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _spremi(c, username, password, uloga):
    return c.post('/api/postavke/korisnici', headers=ORIGIN,
                  json={'username': username, 'password': password, 'uloga': uloga})


# ---- korisnici ----

def test_zadnji_admin_se_ne_moze_degradirati(app, admin):
    r = _spremi(admin, 'admin', 'nova', 'viewer')
    assert r.get_json()['ok'] is False
    assert _rows(app, "SELECT uloga FROM korisnici WHERE username='admin'") == [('admin',)]
    # ni lozinka nije promijenjena — admin i dalje ulazi starom
    c = app.test_client()
    assert c.post('/login', data={'username': 'admin', 'password': 'admin-lozinka'}).status_code == 302


def test_degradacija_dozvoljena_kad_postoji_drugi_admin(app, admin):
    assert _spremi(admin, 'drugi', 'drugi-lozinka', 'admin').get_json()['ok'] is True
    assert _spremi(admin, 'drugi', 'drugi-lozinka', 'viewer').get_json()['ok'] is True
    assert _rows(app, "SELECT uloga FROM korisnici WHERE username='drugi'") == [('viewer',)]
    # sad je 'admin' opet zadnji
    assert _spremi(admin, 'admin', 'x', 'viewer').get_json()['ok'] is False


def test_neaktivni_admin_se_ne_broji(app, admin):
    assert _spremi(admin, 'drugi', 'drugi-lozinka', 'admin').get_json()['ok'] is True
    conn = sqlite3.connect(app.db_path)
    conn.execute("UPDATE korisnici SET aktivan=0 WHERE username='drugi'")
    conn.commit(); conn.close()
    assert _spremi(admin, 'admin', 'x', 'viewer').get_json()['ok'] is False


@pytest.mark.parametrize('uloga', ['root', '', 'Admin', None, 5, ['admin']])
def test_nepoznata_uloga_odbijena(app, admin, uloga):
    r = _spremi(admin, 'novi', 'lozinka', uloga)
    assert r.get_json()['ok'] is False
    assert _rows(app, "SELECT 1 FROM korisnici WHERE username='novi'") == []
    # ni postojecem korisniku se uloga ne moze pokvariti
    assert _spremi(admin, 'gost', 'lozinka', uloga).get_json()['ok'] is False
    assert _rows(app, "SELECT uloga FROM korisnici WHERE username='gost'") == [('viewer',)]


def test_novi_korisnik_default_viewer(app, admin):
    r = admin.post('/api/postavke/korisnici', headers=ORIGIN,
                   json={'username': 'novi', 'password': 'lozinka'})
    assert r.get_json()['ok'] is True
    assert _rows(app, "SELECT uloga, aktivan FROM korisnici WHERE username='novi'") == [('viewer', 1)]


def test_update_cuva_ostale_stupce(app, admin):
    conn = sqlite3.connect(app.db_path)
    conn.execute("UPDATE korisnici SET stvoren='2020-01-01 00:00:00', "
                 "zadnja_prijava='2021-02-03 04:05:06' WHERE username='gost'")
    conn.commit(); conn.close()
    prije = _rows(app, "SELECT id, stvoren, zadnja_prijava, aktivan FROM korisnici WHERE username='gost'")
    assert _spremi(admin, 'gost', 'nova-lozinka', 'admin').get_json()['ok'] is True
    poslije = _rows(app, "SELECT id, stvoren, zadnja_prijava, aktivan FROM korisnici WHERE username='gost'")
    assert prije == poslije
    assert _rows(app, "SELECT uloga FROM korisnici WHERE username='gost'") == [('admin',)]
    assert _rows(app, "SELECT COUNT(*) FROM korisnici") == [(2,)]


def test_prazni_podaci_odbijeni(app, admin):
    assert _spremi(admin, '', 'x', 'viewer').get_json()['ok'] is False
    assert _spremi(admin, 'novi', '', 'viewer').get_json()['ok'] is False
    assert _spremi(admin, None, None, 'viewer').get_json()['ok'] is False


def test_brisanje_zadnjeg_admina_i_dalje_odbijeno(app, admin):
    r = admin.delete('/api/postavke/korisnici?username=admin', headers=ORIGIN)
    assert r.get_json()['ok'] is False
    r = admin.delete('/api/postavke/korisnici?username=gost', headers=ORIGIN)
    assert r.get_json()['ok'] is True


def test_viewer_ne_moze_upravljati_korisnicima(app):
    c = app.test_client()
    c.post('/login', data={'username': 'gost', 'password': 'gost-lozinka'})
    assert c.get('/api/postavke/korisnici').status_code == 403
    assert _spremi(c, 'gost', 'x', 'admin').status_code == 403
    assert c.get('/api/postavke/backup').status_code == 403


# ---- backup ----

def test_backup_download_je_valjana_sqlite_baza(app, admin, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'bk'))
    os.makedirs(tmp_path / 'bk')
    # nezatvorena pisuca konekcija s podacima samo u WAL-u (kao sync kontejner)
    w = sqlite3.connect(app.db_path)
    w.execute("INSERT INTO config (key, value) VALUES ('_wal_only', 'da')")
    w.commit()
    try:
        r = admin.get('/api/postavke/backup')
        assert r.status_code == 200
        blob = r.get_data()
        assert int(r.headers['Content-Length']) == len(blob)
        assert 'attachment' in r.headers['Content-Disposition']
        r.close()
    finally:
        w.close()
    out = tmp_path / 'download.db'
    out.write_bytes(blob)
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute("SELECT value FROM config WHERE key='_wal_only'").fetchone()[0] == 'da'
        assert conn.execute('SELECT COUNT(*) FROM korisnici').fetchone()[0] == 2
    finally:
        conn.close()
    # temp datoteka je pociscena
    assert os.listdir(tmp_path / 'bk') == []


def test_backup_greska_ne_curi_detalje(app, admin, monkeypatch):
    import hepapp.blueprints.postavke as postavke

    def _pukni(dest):
        raise RuntimeError('/tajna/putanja/baza.db is locked')
    monkeypatch.setattr(postavke, '_online_backup', _pukni)
    r = admin.get('/api/postavke/backup')
    assert r.status_code == 500
    assert 'tajna' not in r.get_data(as_text=True)
    r = admin.post('/api/postavke/backup/auto', headers=ORIGIN)
    assert r.get_json()['ok'] is False
    assert 'tajna' not in r.get_data(as_text=True)


def test_backup_auto(app, admin):
    r = admin.post('/api/postavke/backup/auto', headers=ORIGIN)
    d = r.get_json()
    assert d['ok'] is True and os.path.exists(d['path'])
    bdir = os.path.dirname(d['path'])
    assert [f for f in os.listdir(bdir) if not f.endswith('.db')] == []
    conn = sqlite3.connect(d['path'])
    try:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT COUNT(*) FROM korisnici').fetchone()[0] == 2
    finally:
        conn.close()


# ---- poruka o .env ----

def test_spremanje_env_kljuceva_kaze_istinu(app, admin, monkeypatch):
    monkeypatch.setenv('HA_URL', '')     # da os.environ promjena ne procuri u druge testove
    r = admin.post('/api/postavke', headers=ORIGIN, json={'HA_URL': 'https://ha.example.hr'})
    d = r.get_json()
    assert d['ok'] is True
    assert d['env_restart_required'] is True
    assert '.env' in d['warning'] and 'kontejner' in d['warning']


def test_spremanje_samo_db_kljuceva_bez_upozorenja(app, admin):
    r = admin.post('/api/postavke', headers=ORIGIN, json={'PV_INVESTMENT_EUR': '12000'})
    d = r.get_json()
    assert d['ok'] is True
    assert d['env_restart_required'] is False and d['warning'] == ''
    assert _rows(app, "SELECT value FROM config WHERE key='PV_INVESTMENT_EUR'") == [('12000',)]


def test_nepromijenjene_env_vrijednosti_nisu_promjena(app, admin, monkeypatch):
    # UI (savePostavke) uvijek salje i prefillana, nepromijenjena env polja
    monkeypatch.setenv('HEP_USERNAME', 'netko@example.hr')
    monkeypatch.setenv('SMA_PLANT_ID', '12345')
    monkeypatch.setenv('HA_URL', 'https://ha.example.hr')
    tijelo = {'HEP_USERNAME': 'netko@example.hr', 'SMA_PLANT_ID': 12345,
              'HA_URL': 'https://ha.example.hr', 'HEP_SIFRA': '', 'VT_UDIO_PERC': '40'}
    d = admin.post('/api/postavke', headers=ORIGIN, json=tijelo).get_json()
    assert d['ok'] is True
    assert d['env_restart_required'] is False and d['warning'] == ''
    assert _rows(app, "SELECT value FROM config WHERE key='VT_UDIO_PERC'") == [('40',)]
    # i nakon sto su jednom zapisane u .env unutar kontejnera
    d = admin.post('/api/postavke', headers=ORIGIN, json=tijelo).get_json()
    assert d['env_restart_required'] is False
    # stvarna promjena se i dalje prijavljuje
    tijelo['HA_URL'] = 'https://ha2.example.hr'
    d = admin.post('/api/postavke', headers=ORIGIN, json=tijelo).get_json()
    assert d['env_restart_required'] is True and '.env' in d['warning']
    assert 'TARIFA' not in d['warning']


def test_tarifa_kljucevi_ne_obecavaju_env(app, admin, monkeypatch):
    monkeypatch.setenv('TARIFA_VT_OD', '')
    d = admin.post('/api/postavke', headers=ORIGIN, json={'TARIFA_VT_OD': '8'}).get_json()
    assert d['ok'] is True
    assert d['env_restart_required'] is False and d['tarifa_ignored'] is True
    assert 'TARIFA' in d['warning'] and 'NE primjenjuju' in d['warning']
    assert 'force-recreate' not in d['warning']
    # config tablica (ono sto get_config stvarno cita) ostaje netaknuta
    assert _rows(app, "SELECT value FROM config WHERE key='TARIFA_VT_OD'") == [('7',)]
