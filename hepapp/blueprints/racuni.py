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
                    (period, iznos, kwh_plus, kwh_minus, kwh_vt, kwh_nt,
                     opskrba, mreza, pdv, napomena,
                     dug, pretplata, mjerna_mjernina, kamata,
                     datum_racuna, datum_dospijeca, model_tarife,
                     sifra_kupca, broj_racuna)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                d['period'], d['iznos'],
                d.get('kwh_plus'),    d.get('kwh_minus'),
                d.get('kwh_vt'),      d.get('kwh_nt'),
                d.get('opskrba'),     d.get('mreza'),  d.get('pdv'),
                d.get('napomena', ''),
                d.get('dug'),         d.get('pretplata'),
                d.get('mjerna_mjernina'), d.get('kamata'),
                d.get('datum_racuna'),  d.get('datum_dospijeca'),
                d.get('model_tarife'),
                d.get('sifra_kupca'),   d.get('broj_racuna'),
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


@bp.route('/racuni/stanje')
def api_racuni_stanje():
    """Stanje HEP računa: zadnji račun, dug, datum dospijeća, pretplate.

    Vraća podatke iz zadnjeg uvezenog računa + tariff fixne troškove iz HEP_TARIFA.
    """
    from ..tariff import HEP_TARIFA
    conn = get_db()
    try:
        row = conn.execute('''
            SELECT * FROM racuni
            WHERE iznos IS NOT NULL
            ORDER BY datum_racuna DESC, period DESC
            LIMIT 1
        ''').fetchone()
        if not row:
            return jsonify({
                'has_data': False,
                'mjesecna_pretplata': round(HEP_TARIFA['opskrbna_mj'] + HEP_TARIFA['mjerna_mj'], 2),
                'pretplata_opskrba': HEP_TARIFA['opskrbna_mj'],
                'pretplata_mjerna':  HEP_TARIFA['mjerna_mj'],
            })
        r = dict(row)
        pret = (r.get('pretplata') or HEP_TARIFA['opskrbna_mj']) + \
               (r.get('mjerna_mjernina') or HEP_TARIFA['mjerna_mj'])
        return jsonify({
            'has_data':           True,
            'period':             r.get('period'),
            'datum_racuna':       r.get('datum_racuna'),
            'datum_dospijeca':    r.get('datum_dospijeca'),
            'iznos':              r.get('iznos'),
            'dug':                r.get('dug'),
            'kamata':             r.get('kamata'),
            'model_tarife':       r.get('model_tarife'),
            'broj_racuna':        r.get('broj_racuna'),
            'sifra_kupca':        r.get('sifra_kupca'),
            'pretplata_opskrba':  r.get('pretplata') or HEP_TARIFA['opskrbna_mj'],
            'pretplata_mjerna':   r.get('mjerna_mjernina') or HEP_TARIFA['mjerna_mj'],
            'mjesecna_pretplata': round(pret, 2),
            'kwh_plus':           r.get('kwh_plus'),
            'kwh_minus':          r.get('kwh_minus'),
            'kwh_vt':             r.get('kwh_vt'),
            'kwh_nt':             r.get('kwh_nt'),
        })
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
