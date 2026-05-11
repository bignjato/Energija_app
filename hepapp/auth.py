"""Login / logout + LOGIN_PAGE HTML (branding driven by BRAND_* env)."""

import hmac
import logging
import os

from flask import Blueprint, jsonify, redirect, request, session

from .core import hash_password, limiter, needs_rehash, verify_password
from .db import get_db

log = logging.getLogger(__name__)

bp = Blueprint('auth', __name__)

LOGIN_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')


def _brand():
    """Brand strings iz env-a (sve opcionalno)."""
    return {
        'name':     os.environ.get('BRAND_NAME', 'Energy Monitor'),
        'tagline':  os.environ.get('BRAND_TAGLINE', ''),
        'owner':    os.environ.get('BRAND_OWNER', ''),
        'location': os.environ.get('BRAND_LOCATION', ''),
        'email':    os.environ.get('BRAND_EMAIL', ''),
        'phone':    os.environ.get('BRAND_PHONE', ''),
        'web':      os.environ.get('BRAND_WEB', ''),
    }


def _render_login(error: bool = False) -> str:
    b = _brand()
    err_style = 'display:block' if error else 'display:none'
    footer_bits = [b['owner'], b['location'], b['email'], b['phone']]
    footer = ' · '.join(x for x in footer_bits if x) or 'Energy Monitor'
    tagline = b['tagline'] or 'Energetski Monitor'
    owner_line = b['owner'] or ''
    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{b['name']} — Login</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#080c12;color:#d0dde8;font-family:'IBM Plex Sans',sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
.box{{background:#0f1623;border:1px solid #1f2d3d;border-radius:14px;padding:40px 36px;width:100%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,0.5)}}
.logo{{display:flex;align-items:center;justify-content:center;margin-bottom:28px}}
.logo-text{{font-size:32px;font-weight:900;letter-spacing:-1px;line-height:1;color:#22d3ee}}
.subtitle{{font-size:11px;color:#526070;text-align:center;margin-top:4px;text-transform:uppercase;letter-spacing:1px}}
h2{{font-size:16px;font-weight:600;color:#fff;margin-bottom:6px;text-align:center}}
p{{font-size:13px;color:#526070;margin-bottom:24px;text-align:center}}
label{{display:block;font-size:11px;color:#526070;text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px}}
input{{width:100%;padding:11px 14px;background:#080c12;border:1px solid #1f2d3d;
      border-radius:8px;color:#d0dde8;font-size:14px;margin-bottom:16px;font-family:'IBM Plex Mono',monospace;transition:border-color .2s}}
input:focus{{outline:none;border-color:#22d3ee}}
button{{width:100%;padding:12px;background:linear-gradient(135deg,#22d3ee,#0891b2);color:#000;border:none;
       border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.3px;transition:opacity .2s}}
button:hover{{opacity:.9}}
.err{{color:#f87171;font-size:13px;margin-bottom:14px;text-align:center;{err_style};
     background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);border-radius:6px;padding:8px}}
.divider{{border:none;border-top:1px solid #1f2d3d;margin:20px 0}}
.footer{{font-size:11px;color:#526070;text-align:center}}
</style></head>
<body><div class="box">
  <div class="logo">
    <div>
      <div class="logo-text">{b['name'].upper()}</div>
      {f'<div class="subtitle">{b["tagline"]}</div>' if b['tagline'] else ''}
    </div>
  </div>
  <hr class="divider">
  <h2>{tagline}</h2>
  {f'<p>{owner_line}</p>' if owner_line else '<p>&nbsp;</p>'}
  <div class="err" id="err">Pogrešno korisničko ime ili lozinka</div>
  <form method="POST" action="/login">
    <label>Korisničko ime</label>
    <input type="text" name="username" placeholder="korisnik" autocomplete="username" autofocus>
    <label>Lozinka</label>
    <input type="password" name="password" placeholder="••••••••" autocomplete="current-password">
    <button type="submit">→ Prijava</button>
  </form>
  <hr class="divider">
  <div class="footer">{footer}</div>
</div>
</body></html>'''


def get_login_page(error: bool = False) -> str:
    """Render login page with current BRAND_* env values."""
    return _render_login(error)


def _do_login(username: str, pw: str) -> bool:
    if LOGIN_PASSWORD and hmac.compare_digest(pw, LOGIN_PASSWORD):
        session['logged_in'] = True
        session['username']  = username or 'admin'
        session['uloga']     = 'admin'
        session.permanent    = True
        return True
    try:
        conn = get_db()
        user = conn.execute(
            'SELECT id, username, password_hash, uloga FROM korisnici WHERE username=? AND aktivan=1',
            (username,),
        ).fetchone()
        conn.close()
        if user and verify_password(pw, user['password_hash']):
            session['logged_in'] = True
            session['username']  = user['username']
            session['uloga']     = user['uloga']
            session.permanent    = True
            if needs_rehash(user['password_hash']):
                try:
                    c2 = get_db()
                    c2.execute(
                        'UPDATE korisnici SET password_hash=? WHERE id=?',
                        (hash_password(pw), user['id']),
                    )
                    c2.commit()
                    c2.close()
                except Exception as e:
                    log.warning(f'rehash failed: {e}')
            try:
                c2 = get_db()
                c2.execute(
                    'UPDATE korisnici SET zadnja_prijava=datetime("now") WHERE username=?',
                    (user['username'],),
                )
                c2.commit()
                c2.close()
            except Exception:
                pass
            return True
    except Exception as e:
        log.exception(f'Login error: {e}')
    return False


def _login_view():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        pw       = request.form.get('password', '')
        if _do_login(username, pw):
            return redirect('/')
        return _render_login(error=True), 401
    return _render_login(error=False)


if limiter is not None:
    _login_view = limiter.limit('10 per minute; 30 per hour', methods=['POST'])(_login_view)

bp.add_url_rule('/login', 'login_page', _login_view, methods=['GET', 'POST'])


@bp.route('/logout')
def logout():
    session.clear()
    return redirect('/login')
