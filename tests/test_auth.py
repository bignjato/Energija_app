"""Auth testovi: login (ne-ASCII lozinka), CSRF/Origin, re-validacija sesije prema bazi."""

import os
import sqlite3
import tempfile

import pytest

ORIGIN = {'Origin': 'http://localhost'}
VIEW = '/api/postavke/status'     # endpoint dostupan i vieweru


@pytest.fixture
def app(monkeypatch):
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    monkeypatch.setenv('SECRET_KEY', '0123456789abcdef0123456789abcdef')
    monkeypatch.setenv('INITIAL_ADMIN_PASSWORD', 'admin-lozinka')

    import hepapp.db as db
    monkeypatch.setattr(db, 'DB_PATH', path)
    from app import app as flask_app
    import hepapp.auth as auth
    import hepapp.core as core
    db.init_db()   # puna shema + defaultni admin u tmp bazi
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('_setup_complete','1')")
    # tablice koje inace kreira scraper — treba ih VIEW endpoint
    conn.execute('CREATE TABLE IF NOT EXISTS ocitanja_satna (mjerno_mjesto TEXT, ts TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT)')
    conn.execute("INSERT INTO korisnici (username, password_hash, uloga) VALUES (?,?,?)",
                 ('gost', core.hash_password('gost-lozinka'), 'viewer'))
    conn.commit(); conn.close()

    monkeypatch.setattr(auth, 'LOGIN_PASSWORD', '')
    if core.limiter is not None:
        monkeypatch.setattr(core.limiter, 'enabled', False)
    flask_app.config.update(TESTING=True)
    flask_app.db_path = path
    yield flask_app
    for p in (path, path + '-wal', path + '-shm'):
        if os.path.exists(p):
            os.unlink(p)


def _sql(app, sql, args=()):
    conn = sqlite3.connect(app.db_path)
    conn.execute(sql, args)
    conn.commit(); conn.close()


def _login(app, username, password):
    c = app.test_client()
    r = c.post('/login', data={'username': username, 'password': password})
    return c, r


# ---- login ----

def test_login_ok_i_pogresna_lozinka(app):
    c, r = _login(app, 'admin', 'admin-lozinka')
    assert r.status_code == 302
    assert c.get('/api/postavke/korisnici').status_code == 200
    _, r = _login(app, 'admin', 'kriva')
    assert r.status_code == 401


def test_ne_ascii_lozinka_env_mode_ne_puca(app, monkeypatch):
    import hepapp.auth as auth
    monkeypatch.setattr(auth, 'LOGIN_PASSWORD', 'tajna')
    _, r = _login(app, 'admin', 'šđčćž')
    assert r.status_code == 401          # prije: TypeError => 500

    monkeypatch.setattr(auth, 'LOGIN_PASSWORD', 'lozinkaš')
    c, r = _login(app, '', 'lozinkaš')
    assert r.status_code == 302
    # env sesija (bez korisnika u bazi) i dalje radi kao admin
    assert c.get('/api/postavke/korisnici').status_code == 200


def test_ne_ascii_lozinka_db_korisnik(app):
    import hepapp.core as core
    _sql(app, "UPDATE korisnici SET password_hash=? WHERE username='gost'",
         (core.hash_password('čokolada-đ'),))
    c, r = _login(app, 'gost', 'čokolada-đ')
    assert r.status_code == 302
    assert c.get(VIEW).status_code == 200
    _, r = _login(app, 'gost', 'cokolada-d')
    assert r.status_code == 401


def test_env_sesija_pada_kad_se_env_lozinka_promijeni(app, monkeypatch):
    import hepapp.auth as auth
    monkeypatch.setattr(auth, 'LOGIN_PASSWORD', 'stara-env')
    c, _ = _login(app, 'bilo-tko', 'stara-env')
    assert c.get('/api/postavke/korisnici').status_code == 200
    monkeypatch.setattr(auth, 'LOGIN_PASSWORD', 'nova-env')
    assert c.get('/api/postavke/korisnici').status_code == 401
    monkeypatch.setattr(auth, 'LOGIN_PASSWORD', '')
    assert c.get('/api/postavke/korisnici').status_code == 401


def test_stari_hash_se_rehashira_i_sesija_ostaje_valjana(app):
    import hashlib
    _sql(app, "UPDATE korisnici SET password_hash=? WHERE username='gost'",
         (hashlib.sha256(b'gost-lozinka').hexdigest(),))
    c, r = _login(app, 'gost', 'gost-lozinka')
    assert r.status_code == 302
    conn = sqlite3.connect(app.db_path)
    h = conn.execute("SELECT password_hash FROM korisnici WHERE username='gost'").fetchone()[0]
    conn.close()
    assert h.startswith('pbkdf2$')
    assert c.get(VIEW).status_code == 200


# ---- CSRF / Origin ----

def _post_korisnik(c, headers):
    return c.post('/api/postavke/korisnici', headers=headers,
                  json={'username': 'novi', 'password': 'x12345', 'uloga': 'viewer'})


def test_origin_tocan_prolazi_slican_host_ne(app):
    c, _ = _login(app, 'admin', 'admin-lozinka')
    assert _post_korisnik(c, {'Origin': 'http://localhost'}).status_code == 200
    assert _post_korisnik(c, {'Origin': 'http://localhost:80'}).status_code == 200
    for zao in ('http://localhost.evil.com', 'http://localhost@evil.com',
                'http://localhostevil.com', 'https://localhost', 'http://localhost:8080',
                'null', 'http://[::1'):
        r = _post_korisnik(c, {'Origin': zao})
        assert r.status_code == 403, zao


def test_origin_iza_proxyja(app):
    c, _ = _login(app, 'admin', 'admin-lozinka')
    px = {'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'energija.example.hr'}
    assert _post_korisnik(c, {**px, 'Origin': 'https://energija.example.hr'}).status_code == 200
    assert _post_korisnik(c, {**px, 'Origin': 'https://energija.example.hr.evil.com'}).status_code == 403
    assert _post_korisnik(c, {**px, 'Origin': 'http://energija.example.hr'}).status_code == 403
    # Referer fallback (bez Origin headera) radi kao i prije
    assert _post_korisnik(c, {**px, 'Referer': 'https://energija.example.hr/postavke'}).status_code == 200
    assert _post_korisnik(c, {**px, 'Referer': 'https://energija.example.hr.evil.com/x'}).status_code == 403


def test_bez_origina_token_prolazi_bez_icega_ne(app):
    c, _ = _login(app, 'admin', 'admin-lozinka')
    c.get('/api/postavke/korisnici')
    with c.session_transaction() as s:
        tok = s['csrf_token']
    assert _post_korisnik(c, {'X-CSRF-Token': tok}).status_code == 200
    assert _post_korisnik(c, {}).status_code == 403
    assert _post_korisnik(c, {'X-CSRF-Token': 'krivi'}).status_code == 403
    # ne-ASCII token ne smije dati 500
    assert _post_korisnik(c, {'X-CSRF-Token': 'š'.encode('utf-8').decode('latin-1')}).status_code == 403


# ---- re-validacija sesije ----

def test_obrisani_korisnik_gubi_pristup(app):
    c, _ = _login(app, 'gost', 'gost-lozinka')
    assert c.get(VIEW).status_code == 200
    _sql(app, "DELETE FROM korisnici WHERE username='gost'")
    assert c.get(VIEW).status_code == 401
    # sesija je ociscena — stranica vraca login formu
    assert 'name="password"' in c.get('/').get_data(as_text=True)
    with c.session_transaction() as s:
        assert not s.get('logged_in')


def test_deaktivirani_korisnik_gubi_pristup(app):
    c, _ = _login(app, 'gost', 'gost-lozinka')
    _sql(app, "UPDATE korisnici SET aktivan=0 WHERE username='gost'")
    assert c.get(VIEW).status_code == 401


def test_degradirani_admin_gubi_admin_prava(app):
    import hepapp.core as core
    _sql(app, "INSERT INTO korisnici (username, password_hash, uloga) VALUES (?,?,?)",
         ('drugi', core.hash_password('drugi-lozinka'), 'admin'))
    c, _ = _login(app, 'drugi', 'drugi-lozinka')
    assert c.get('/api/postavke/korisnici').status_code == 200
    _sql(app, "UPDATE korisnici SET uloga='viewer' WHERE username='drugi'")
    assert c.get('/api/postavke/korisnici').status_code == 403
    assert c.get(VIEW).status_code == 200          # i dalje prijavljen
    with c.session_transaction() as s:
        assert s['uloga'] == 'viewer'


def test_unaprijedjeni_viewer_dobiva_admin(app):
    c, _ = _login(app, 'gost', 'gost-lozinka')
    assert c.get('/api/postavke/korisnici').status_code == 403
    _sql(app, "UPDATE korisnici SET uloga='admin' WHERE username='gost'")
    assert c.get('/api/postavke/korisnici').status_code == 200


def test_promjena_lozinke_invalidira_staru_sesiju(app):
    stara, _ = _login(app, 'gost', 'gost-lozinka')
    assert stara.get(VIEW).status_code == 200
    adm, _ = _login(app, 'admin', 'admin-lozinka')
    r = adm.post('/api/postavke/korisnici', headers=ORIGIN,
                 json={'username': 'gost', 'password': 'nova-lozinka', 'uloga': 'viewer'})
    assert r.get_json()['ok'] is True
    assert stara.get(VIEW).status_code == 401
    nova, r = _login(app, 'gost', 'nova-lozinka')
    assert r.status_code == 302 and nova.get(VIEW).status_code == 200


def test_vlastita_promjena_lozinke_rusi_druge_sesije_ali_ne_trenutnu(app):
    a1, _ = _login(app, 'admin', 'admin-lozinka')
    a2, _ = _login(app, 'admin', 'admin-lozinka')
    r = a1.post('/api/postavke/korisnici', headers=ORIGIN,
                json={'username': 'admin', 'password': 'nova-admin', 'uloga': 'admin'})
    assert r.get_json()['ok'] is True
    assert a1.get('/api/postavke/korisnici').status_code == 200
    assert a2.get('/api/postavke/korisnici').status_code == 401


def test_zahtjev_sa_starim_otiskom_ne_dira_cookie(app):
    # Zahtjev "u letu" sa starim cookiejem, provjeren nakon vlastite promjene
    # lozinke: 401, ali bez Set-Cookie za sesiju — inace bi zakasnjeli odgovor
    # pregazio svjezi cookie iz POST-a i odjavio admina.
    a1, _ = _login(app, 'admin', 'admin-lozinka')
    stari = a1.get_cookie('session').value
    r = a1.post('/api/postavke/korisnici', headers=ORIGIN,
                json={'username': 'admin', 'password': 'nova-admin', 'uloga': 'admin'})
    assert r.get_json()['ok'] is True
    novi = a1.get_cookie('session').value
    assert novi != stari

    u_letu = app.test_client()
    u_letu.set_cookie('session', stari)
    for put in ('/api/postavke/korisnici', '/'):
        r = u_letu.get(put)
        assert not any(h.startswith('session=') for h in r.headers.getlist('Set-Cookie'))
    assert u_letu.get('/api/postavke/korisnici').status_code == 401
    # stari cookie i dalje ne vrijedi, svjezi vrijedi
    assert a1.get('/api/postavke/korisnici').status_code == 200
    # ponovna prijava preko starog cookieja radi normalno
    r = u_letu.post('/login', data={'username': 'admin', 'password': 'nova-admin'})
    assert r.status_code == 302 and u_letu.get('/api/postavke/korisnici').status_code == 200


def test_obrisani_korisnik_dobiva_brisanje_cookieja(app):
    c, _ = _login(app, 'gost', 'gost-lozinka')
    _sql(app, "DELETE FROM korisnici WHERE username='gost'")
    r = c.get(VIEW)
    assert r.status_code == 401
    assert any(h.startswith('session=;') for h in r.headers.getlist('Set-Cookie'))


def test_stara_sesija_bez_otiska_trazi_ponovnu_prijavu(app):
    c = app.test_client()
    with c.session_transaction() as s:      # cookie kakav je izdavala stara verzija
        s['logged_in'] = True
        s['username'] = 'admin'
        s['uloga'] = 'admin'
    assert c.get('/api/postavke/korisnici').status_code == 401
    r = c.get('/')
    assert r.status_code == 200 and 'name="password"' in r.get_data(as_text=True)


def test_krivotvoreni_env_marker_bez_env_lozinke_ne_prolazi(app):
    c, _ = _login(app, 'gost', 'gost-lozinka')
    with c.session_transaction() as s:
        s['auth_src'] = 'env'
        s['uloga'] = 'admin'
    assert c.get('/api/postavke/korisnici').status_code == 401


def test_free_paths_i_neprijavljeni(app):
    c = app.test_client()
    assert c.get('/login').status_code == 200
    assert c.get(VIEW).status_code == 401
    assert c.get('/logout').status_code == 302
