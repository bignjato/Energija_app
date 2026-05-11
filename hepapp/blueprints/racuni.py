"""/api/racuni — CRUD HEP računa + PDF upload."""

import logging

from flask import Blueprint, jsonify, request, session

from ..bill_parser import extract_text, parse_hep_bill
from ..core import admin_required
from ..db import get_db

log = logging.getLogger(__name__)

bp = Blueprint('racuni', __name__, url_prefix='/api')

MAX_PDF_BYTES = 5 * 1024 * 1024  # 5 MB


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


@bp.route('/racuni/upload-pdf', methods=['POST'])
@admin_required
def api_upload_pdf():
    """Parsiraj HEP PDF račun, vrati prepoznata polja za korisničku potvrdu.
    NE sprema u bazu — frontend pokaže preview pa pozove /api/racuni POST.
    """
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'Pošalji PDF kao multipart field "file"'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'error': 'Prazno ime datoteke'}), 400
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'error': 'Samo PDF datoteke su podržane'}), 400

    pdf_bytes = f.read(MAX_PDF_BYTES + 1)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        return jsonify({'ok': False, 'error': f'PDF prevelik (>{MAX_PDF_BYTES // 1024 // 1024} MB)'}), 413

    try:
        text = extract_text(pdf_bytes)
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    except Exception as e:
        log.exception('PDF extract failed')
        return jsonify({'ok': False, 'error': f'Ne mogu pročitati PDF: {e}'}), 400

    parsed = parse_hep_bill(text)
    parsed['ok'] = True
    parsed['filename'] = f.filename
    return jsonify(parsed)
