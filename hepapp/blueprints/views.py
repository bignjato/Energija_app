"""Root + health endpoints."""

import os

from flask import Blueprint

bp = Blueprint('views', __name__)

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '..', 'dashboard_template.html',
)
TEMPLATE_PATH = os.path.abspath(TEMPLATE_PATH)

_DASHBOARD_CACHE = None


def _get_dashboard():
    global _DASHBOARD_CACHE
    if _DASHBOARD_CACHE is None:
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            _DASHBOARD_CACHE = f.read()
    return _DASHBOARD_CACHE


@bp.route('/health')
def health():
    return 'OK', 200


@bp.route('/')
def index():
    return _get_dashboard()
