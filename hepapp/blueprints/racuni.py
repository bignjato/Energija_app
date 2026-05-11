"""/api/racuni — CRUD HEP računa."""

from flask import Blueprint, jsonify, request, session

from ..db import get_db

bp = Blueprint('racuni', __name__, url_prefix='/api')


@bp.route('/racuni', methods=['GET', 'POST', 'DELETE'])
def api_racuni():
    if request.method != 'GET' and session.get('uloga') != 'admin':
        return jsonify({'error': 'Forbidden — admin only'}), 403
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute('SELECT * FROM racuni ORDER BY period DESC').fetchall()
            return jsonify([dict(r) for r in rows])
        if request.method == 'POST':
            d = request.get_json() or {}
            conn.execute('''
                INSERT OR REPLACE INTO racuni
                    (period, iznos, kwh_plus, kwh_minus, kwh_vt, kwh_nt, opskrba, mreza, pdv, napomena)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', (
                d['period'], d['iznos'],
                d.get('kwh_plus'), d.get('kwh_minus'),
                d.get('kwh_vt'),   d.get('kwh_nt'),
                d.get('opskrba'),  d.get('mreza'), d.get('pdv'),
                d.get('napomena', ''),
            ))
            conn.commit()
            return jsonify({'ok': True})
        # DELETE
        period = request.args.get('period')
        if period:
            conn.execute('DELETE FROM racuni WHERE period=?', (period,))
            conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()
