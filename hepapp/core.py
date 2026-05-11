"""Cross-cutting helpers: password hashing, decorators, CSRF, rate limiter."""

import functools
import hashlib
import hmac
import logging
import secrets

from flask import jsonify, request, session

log = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 200_000

# Rate limiter — populated by init_limiter
limiter = None


def init_limiter(app):
    global limiter
    try:
        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address
        limiter = Limiter(
            app=app, key_func=get_remote_address,
            default_limits=[], storage_uri='memory://',
        )
    except ImportError:
        log.warning('flask-limiter nije instaliran — rate limiting onemogućen.')
        limiter = None
    return limiter


# ===== PASSWORD =====

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
            return hmac.compare_digest(test, h)
        if ':' in stored:
            salt, h = stored.split(':', 1)
            test = hashlib.sha256(f'{salt}:{pw}'.encode()).hexdigest()
            return hmac.compare_digest(test, h)
        return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    return not (stored or '').startswith('pbkdf2$')


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

def csrf_check():
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return None
    origin = request.headers.get('Origin') or request.headers.get('Referer') or ''
    host = request.host_url.rstrip('/')
    if not origin or not origin.startswith(host):
        return jsonify({'error': 'CSRF check failed'}), 403
    return None


# ===== BEFORE-REQUEST =====

FREE_PATHS    = ['/login', '/logout', '/health', '/favicon.ico']
WIZARD_PATHS  = ['/setup', '/api/setup/complete', '/logout']


def register_before_request(app):
    from flask import redirect
    from .db import get_config

    @app.before_request
    def _check():
        from .auth import LOGIN_PAGE
        if request.path in FREE_PATHS or request.path.startswith('/static/'):
            return None
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return LOGIN_PAGE

        # First-run wizard: admin && !setup_complete → /setup
        if session.get('uloga') == 'admin':
            if get_config('_setup_complete', '0') != '1':
                if request.path not in WIZARD_PATHS and not request.path.startswith('/static/'):
                    if request.path.startswith('/api/'):
                        return jsonify({'error': 'Setup wizard nije završen — idi na /setup'}), 409
                    return redirect('/setup')

        return csrf_check()
