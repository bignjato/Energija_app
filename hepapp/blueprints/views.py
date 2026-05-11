"""Root + health endpoints + dashboard HTML rendering."""

import os

from flask import Blueprint

bp = Blueprint('views', __name__)

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'dashboard_template.html',
)

_DASHBOARD_CACHE = None


def _brand_tokens() -> dict:
    """BRAND_* env varovi za zamjenu u template-u."""
    name = os.environ.get('BRAND_NAME', 'Energy Monitor')
    owner = os.environ.get('BRAND_OWNER', '')
    location = os.environ.get('BRAND_LOCATION', '')
    tagline = os.environ.get('BRAND_TAGLINE', 'HEP ODS energy dashboard')
    email = os.environ.get('BRAND_EMAIL', '')
    phone = os.environ.get('BRAND_PHONE', '')
    web = os.environ.get('BRAND_WEB', '')

    # Owner line: "Owner · Location · Tagline" — drop empty parts
    owner_bits = [b for b in (owner, location, tagline) if b]
    owner_line = ' · '.join(owner_bits) if owner_bits else tagline

    # Web link HTML (samo ako je BRAND_WEB postavljen)
    web_html = ''
    if web:
        web_url = web if web.startswith('http') else f'https://{web}'
        web_clean = web.replace('https://', '').replace('http://', '').rstrip('/')
        web_html = f'<a href="{web_url}" target="_blank" style="color:var(--accent);text-decoration:none">🌐 {web_clean}</a>'

    email_html = f'<a href="mailto:{email}" style="color:var(--muted);text-decoration:none">✉ {email}</a>' if email else ''

    phone_html = ''
    if phone:
        phone_tel = phone.replace(' ', '').replace('+', '')
        phone_html = f'<a href="tel:+{phone_tel}" style="color:var(--muted);text-decoration:none">📞 {phone}</a>'

    contact_block = ''
    if web_html or email_html or phone_html:
        contact_block = (
            '<div style="display:flex;flex-direction:column;gap:2px;padding:0 12px;'
            'border-right:1px solid var(--border);font-size:10px;font-family:var(--mono)">'
            f'{web_html}{email_html}{phone_html}</div>'
        )

    return {
        'BRAND_NAME':         name,
        'BRAND_NAME_HEADER':  name.upper(),
        'BRAND_TAGLINE':      tagline,
        'BRAND_OWNER_LINE':   owner_line,
        'BRAND_CONTACT_HTML': contact_block,
    }


def _get_dashboard() -> str:
    """Load dashboard HTML s brand tokenima zamijenjenim."""
    global _DASHBOARD_CACHE
    if _DASHBOARD_CACHE is None:
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            html = f.read()
        for k, v in _brand_tokens().items():
            html = html.replace('{{' + k + '}}', v)
        _DASHBOARD_CACHE = html
    return _DASHBOARD_CACHE


def invalidate_cache():
    """Pozovi kad se brand env vars mijenjaju da se cache regenerira."""
    global _DASHBOARD_CACHE
    _DASHBOARD_CACHE = None


@bp.route('/health')
def health():
    return 'OK', 200


@bp.route('/')
def index():
    return _get_dashboard()
