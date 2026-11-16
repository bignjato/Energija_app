"""Cross-cutting helpers: password hashing, decorators, CSRF, rate limiter."""

import functools
import hashlib
import hmac
import logging
import os
import secrets
from urllib.parse import urlsplit

from flask import current_app, g, jsonify, request, session
from flask.sessions import SecureCookieSessionInterface

log = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 200_000

# Rate limiter — populated by init_limiter
limiter = None


def init_limiter(app):
    global limiter
    try:
        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address
        # memory:// brojaci su per-gunicorn-worker (-w 2 => efektivni limit x2).
        # Svjesno prihvaceno: i dvostruki login limit je dovoljno restriktivan.
        limiter = Limiter(
            app=app, key_func=get_remote_address,
            default_limits=[], storage_uri='memory://',
        )
    except ImportError:
        log.warning('flask-limiter nije instaliran — rate limiting onemogućen.')
        limiter = None
    return limiter


# ===== PASSWORD =====

def _eq(a: str, b: str) -> bool:
    """Constant-time usporedba; bytes jer compare_digest na ne-ASCII str baca TypeError."""
    return hmac.compare_digest((a or '').encode('utf-8'), (b or '').encode('utf-8'))


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()
    return f'pbkdf2${PBKDF2_ITERATIONS}${salt}${h}'


def verify_password(pw: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        if stored.startswith('pbkdf2$'):
            _, iter_s, salt, h = stored.split('$', 3)
            test = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), int(iter_s)).hex()
            return _eq(test, h)
        if ':' in stored:
            salt, h = stored.split(':', 1)
            test = hashlib.sha256(f'{salt}:{pw}'.encode()).hexdigest()
            return _eq(test, h)
        return _eq(hashlib.sha256(pw.encode()).hexdigest(), stored)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    return not (stored or '').startswith('pbkdf2$')


# ===== SESIJA =====

def session_fingerprint(secret: str) -> str:
    """Kratki otisak hasha lozinke (HMAC app secretom) koji se drzi u sesiji.

    Promjena lozinke mijenja hash => otisak vise ne odgovara => stare sesije padaju.
    Cookie je potpisan ali citljiv, zato HMAC a ne sam hash.
    """
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode('utf-8')
    return hmac.new(key, (secret or '').encode('utf-8'), hashlib.sha256).hexdigest()[:16]


def start_session(sess, username: str, uloga: str, secret: str, src: str = 'db'):
    """Oznaci sesiju prijavljenom. secret = password_hash iz baze (src='db')
    odnosno DASHBOARD_PASSWORD (src='env', prijava bez korisnika u bazi)."""
    sess['logged_in'] = True
    sess['username']  = username
    sess['uloga']     = uloga
    sess['auth_src']  = src
    sess['pwfp']      = session_fingerprint(secret)
    sess.permanent    = True


SESIJA_OK, SESIJA_NEVALJANA, SESIJA_STARA_LOZINKA = 'ok', 'nevaljana', 'stara_lozinka'


def _session_valid(conn) -> str:
    """Provjeri sesiju prema bazi (jedan lookup po UNIQUE username indeksu)
    i osvjezi ulogu. Sesije bez otiska (stare) => nevaljana => ponovna prijava.
    Korisnik postoji ali otisak ne odgovara => SESIJA_STARA_LOZINKA (vidi _check)."""
    fp = session.get('pwfp') or ''
    if not fp:
        return SESIJA_NEVALJANA
    if session.get('auth_src') == 'env':
        from . import auth as _auth
        pw = _auth.LOGIN_PASSWORD
        ok = (bool(pw) and session.get('uloga') == 'admin'
              and _eq(fp, session_fingerprint(pw)))
        return SESIJA_OK if ok else SESIJA_NEVALJANA
    row = conn.execute(
        'SELECT password_hash, uloga FROM korisnici WHERE username=? AND aktivan=1',
        (session.get('username') or '',),
    ).fetchone()
    if not row:
        return SESIJA_NEVALJANA
    if not _eq(fp, session_fingerprint(row['password_hash'])):
        return SESIJA_STARA_LOZINKA
    if session.get('uloga') != row['uloga']:
        session['uloga'] = row['uloga']
    return SESIJA_OK


class SessionInterface(SecureCookieSessionInterface):
    """Kao defaultni, ali odgovor na zahtjev sa starim otiskom lozinke ne dira cookie."""

    def should_set_cookie(self, app, session):
        if g.get('_bez_session_cookieja'):
            return False
        return super().should_set_cookie(app, session)


# ===== DECORATORS =====

def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get('logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get('logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('uloga') != 'admin':
            return jsonify({'error': 'Forbidden — admin only'}), 403
        return fn(*a, **kw)
    return wrapper


# ===== CSRF =====

_DEFAULT_PORTS = {'http': 80, 'https': 443}


def _origin_of(url: str):
    """(scheme, host, port) iz URL-a ili None; defaultni port => None."""
    try:
        p = urlsplit(url)
        scheme = (p.scheme or '').lower()
        host = (p.hostname or '').lower()
        port = p.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if port == _DEFAULT_PORTS[scheme]:
        port = None
    return (scheme, host, port)


def csrf_check():
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return None
    # Per-session token (X-CSRF-Token header) pokriva klijente bez
    # Origin/Referer headera; Origin provjera ostaje kao fallback.
    token = request.headers.get('X-CSRF-Token', '')
    expected = session.get('csrf_token', '')
    if token and expected and _eq(token, expected):
        return None
    origin = request.headers.get('Origin') or request.headers.get('Referer') or ''
    # Egzaktna usporedba scheme+host[:port] — startswith bi propustio
    # 'https://nas-host.evil.com'. host_url vec postuje ProxyFix (X-Forwarded-*).
    ours = _origin_of(request.host_url)
    if not origin or ours is None or _origin_of(origin) != ours:
        return jsonify({'error': 'CSRF check failed'}), 403
    return None


# ===== BEFORE-REQUEST =====

FREE_PATHS    = ['/login', '/logout', '/health', '/favicon.ico']
WIZARD_PATHS  = ['/setup', '/api/setup/complete', '/logout']


def register_before_request(app):
    from flask import redirect
    from .db import get_db

    def _odbij():
        from .auth import get_login_page
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return get_login_page()

    def _setup_complete(conn) -> bool:
        # isto sto i get_config('_setup_complete', '0'), ali na vec otvorenoj konekciji
        try:
            row = conn.execute("SELECT value FROM config WHERE key='_setup_complete'").fetchone()
        except Exception:
            row = None
        return (row[0] if row else os.environ.get('_setup_complete', '0')) == '1'

    @app.before_request
    def _check():
        if request.path in FREE_PATHS or request.path.startswith('/static/'):
            return None
        if not session.get('logged_in'):
            return _odbij()

        # Jedna konekcija po requestu: provjera sesije + _setup_complete.
        conn = None
        try:
            # Potpisani cookie nije dovoljan: korisnik je u medjuvremenu mogao biti
            # obrisan, degradiran ili mu je promijenjena lozinka.
            try:
                conn = get_db()
                stanje = _session_valid(conn)
            except Exception:
                # Baza nedostupna: fail-closed, ali ne brisi sesiju (prolazna greska)
                log.exception('Provjera sesije nije uspjela')
                return jsonify({'error': 'Servis trenutno nedostupan'}), 503
            if stanje == SESIJA_STARA_LOZINKA:
                # Lozinka promijenjena nakon izdavanja cookieja. Sesija je klijentski
                # cookie: zahtjev koji je vec bio u letu kad je korisnik sam sebi
                # promijenio lozinku ne smije brisanjem (ni ponovnim slanjem starog)
                # pregaziti svjezi cookie iz tog POST-a — zato odgovor bez Set-Cookie.
                # Stari cookie je ionako bezvrijedan, pada na svakom zahtjevu.
                # Ne pokriva zahtjev provjeren PRIJE commita ciji odgovor stigne
                # nakon POST-a (rijetko; posljedica je samo ponovna prijava).
                g._bez_session_cookieja = True
                return _odbij()
            if stanje != SESIJA_OK:
                session.clear()
                return _odbij()

            if 'csrf_token' not in session:
                session['csrf_token'] = secrets.token_hex(16)

            # First-run wizard: admin && !setup_complete → /setup
            if session.get('uloga') == 'admin':
                if not _setup_complete(conn):
                    if request.path not in WIZARD_PATHS and not request.path.startswith('/static/'):
                        if request.path.startswith('/api/'):
                            return jsonify({'error': 'Setup wizard nije završen — idi na /setup'}), 409
                        return redirect('/setup')
        finally:
            if conn is not None:
                conn.close()

        return csrf_check()

    @app.after_request
    def _csrf_cookie(resp):
        # Ne-HttpOnly kopija tokena da je frontend fetch wrapper moze citati
        tok = session.get('csrf_token')
        if tok and request.cookies.get('csrf_token') != tok:
            resp.set_cookie('csrf_token', tok, httponly=False, samesite='Lax',
                            secure=app.config.get('SESSION_COOKIE_SECURE', True))
        return resp
