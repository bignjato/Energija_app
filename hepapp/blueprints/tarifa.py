"""/api/tarifa (GET/POST)"""

from flask import Blueprint, jsonify, request, session

from ..db import get_db

bp = Blueprint('tarifa', __name__, url_prefix='/api')


@bp.route('/tarifa', methods=['GET', 'POST'])
def api_tarifa():
    if request.method == 'POST' and session.get('uloga') != 'admin':
        return jsonify({'error': 'Forbidden — admin only'}), 403
    conn = get_db()
    try:
        if request.method == 'POST':
            data = request.get_json() or {}
            conn.execute('UPDATE tarife SET aktivan=0')
            conn.execute('''
                INSERT INTO tarife (naziv, cijena_kupnja, cijena_prodaja, vt_pocetak, vt_kraj, aktivan)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (
                data.get('naziv', 'Moja tarifa'),
                float(data.get('cijena_kupnja', 0.12)),
                float(data.get('cijena_prodaja', 0.065)),
                int(data.get('vt_pocetak', 7)),
                int(data.get('vt_kraj', 21)),
            ))
            conn.commit()
            return jsonify({'ok': True})
        rows = conn.execute('SELECT * FROM tarife ORDER BY id DESC').fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()
