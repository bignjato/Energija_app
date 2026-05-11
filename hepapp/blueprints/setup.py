"""/api/setup/* — pokretanje scrapera (admin).

NAPOMENA: scraperi žive u sync kontejneru, web kontejner ih ne vidi preko
subprocess. Ove rute zadržane su radi UI-ja; u trenutnoj topologiji za
trenutni rad rebootaj sync kontejner ili pričekaj idući 5-min ciklus.
"""

import subprocess

from flask import Blueprint, jsonify, request

from ..core import admin_required

bp = Blueprint('setup', __name__, url_prefix='/api/setup')


def _run(args, timeout):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        lines = (result.stdout + result.stderr).strip().split('\n')
        return {'ok': True, 'info': lines[-1] if lines else 'Gotovo'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@bp.route('/import-hep', methods=['POST'])
@admin_required
def api_import_hep():
    dani = request.args.get('dani', '30')
    try:
        n = int(dani)
        if n < 1 or n > 3650:
            raise ValueError
    except ValueError:
        return jsonify({'ok': False, 'error': 'dani mora biti broj 1–3650'}), 400
    return jsonify(_run(['python3', '/app/hep_scraper.py', '--dani', str(n)], 300))


@bp.route('/import-sma', methods=['POST'])
@admin_required
def api_import_sma():
    return jsonify(_run(['python3', '/app/sma_history_import.py'], 600))


@bp.route('/sync-ha', methods=['POST'])
@admin_required
def api_sync_ha():
    return jsonify(_run(['python3', '/app/ha_sender.py'], 60))
