"""/api/racuni — CRUD HEP računa + PDF upload."""

import json
import logging
import re
import sqlite3

from flask import Blueprint, jsonify, request, session

from ..bill_parser import extract_text, missing_key_fields, parse_hep_bill
from ..core import admin_required
from ..db import get_db
from ..tariff import invalidate_tarifa_cache

log = logging.getLogger(__name__)

bp = Blueprint('racuni', __name__, url_prefix='/api')

MAX_PDF_BYTES = 5 * 1024 * 1024  # 5 MB


def _period_iso(period):
    """'MM/YYYY' (parser) ili 'YYYY-MM' (frontend POST) → 'YYYY-MM'."""
    p = str(period or '').strip()
    m = re.fullmatch(r'(\d{1,2})/(\d{4})', p)
    return f'{m.group(2)}-{int(m.group(1)):02d}' if m else p


def _cijene_json(cijene):
    """Cijene s računa → JSON za racuni.cijene_json (None ako ih nema).
    Raspon vrijednosti provjerava tariff.get_tarifa pri čitanju."""
    if not isinstance(cijene, dict):
        return None
    ciste = {str(k): v for k, v in cijene.items()
             if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return json.dumps(ciste, sort_keys=True) if ciste else None


def _zapamti_cijene_pdf(period, cijene, broj_racuna):
    """Sačuvaj cijene s upravo parsiranog PDF-a dok korisnik ne potvrdi račun.

    Frontend u POST /api/racuni šalje samo polja forme (bez cijena), pa ih
    spremanje pokupi odavde po periodu. U bazi, a ne u memoriji procesa —
    upload i spremanje mogu završiti na različitim gunicorn workerima.
    """
    cj = _cijene_json(cijene)
    period = _period_iso(period)
    if not cj or not period:
        return
    try:
        conn = get_db()
        try:
            conn.execute(
                'INSERT OR REPLACE INTO racuni_cijene_pdf (period, cijene_json, broj_racuna) '
                'VALUES (?,?,?)', (period, cj, broj_racuna))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning('Cijene s PDF računa %s nisu sačuvane za spremanje: %s', period, e)


def _cijene_za_spremanje(conn, d):
    """cijene_json za račun koji se sprema: iz POST-a → s parsiranog PDF-a →
    već spremljene za taj period (ručni ispravak iznosa ne smije obrisati cijene)."""
    cj = _cijene_json(d.get('cijene'))
    if cj:
        return cj
    # sqlite3.Error = stara shema (init_db/migracija v2 nije prošla) — račun se
    # tada sprema bez cijena, kao prije.
    try:
        row = conn.execute(
            'SELECT cijene_json, broj_racuna FROM racuni_cijene_pdf WHERE period=?',
            (d['period'],)).fetchone()
        # Ako oba imaju broj računa, mora biti isti (PDF jednog, ručni unos drugog računa).
        if row and (not row['broj_racuna'] or not d.get('broj_racuna')
                    or row['broj_racuna'] == d.get('broj_racuna')):
            return row['cijene_json']
    except sqlite3.Error:
        pass
    try:
        row = conn.execute(
            'SELECT cijene_json FROM racuni WHERE period=?', (d['period'],)).fetchone()
        return row['cijene_json'] if row else None
    except sqlite3.Error:
        return None


def _ima_kolonu_cijene(conn) -> bool:
    return any(r[1] == 'cijene_json' for r in conn.execute('PRAGMA table_info(racuni)'))


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
            cijene_json = _cijene_za_spremanje(conn, d)
            polja = (
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
            )
            kolone = '''period, iznos, kwh_plus, kwh_minus, kwh_vt, kwh_nt,
                     opskrba, mreza, pdv, napomena,
                     dug, pretplata, mjerna_mjernina, kamata,
                     datum_racuna, datum_dospijeca, model_tarife,
                     sifra_kupca, broj_racuna'''
            # Tablica racuni_cijene_pdf može postojati i kad ALTER za
            # cijene_json nije prošao (npr. zaključana baza pri startu) —
            # račun se tada sprema bez cijena, a zapamćene cijene s PDF-a
            # ostaju za ponovno spremanje nakon migracije.
            if cijene_json and not _ima_kolonu_cijene(conn):
                log.warning('racuni.cijene_json ne postoji (migracija nije prošla) — '
                            'račun %s spremam bez cijena', d['period'])
                cijene_json = None
            if cijene_json:
                conn.execute(
                    f'INSERT OR REPLACE INTO racuni ({kolone}, cijene_json) '
                    f'VALUES ({",".join("?" * 20)})', polja + (cijene_json,))
                try:
                    conn.execute('DELETE FROM racuni_cijene_pdf WHERE period=?', (d['period'],))
                except sqlite3.Error:
                    pass
            else:
                conn.execute(
                    f'INSERT OR REPLACE INTO racuni ({kolone}) '
                    f'VALUES ({",".join("?" * 19)})', polja)
            conn.commit()
            invalidate_tarifa_cache()
            return jsonify({'ok': True})
        # DELETE
        period = request.args.get('period')
        if period:
            conn.execute('DELETE FROM racuni WHERE period=?', (period,))
            conn.commit()
            invalidate_tarifa_cache()
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
    NE sprema račun — frontend pokaže preview pa pozove /api/racuni POST.
    Jedinične cijene s računa se pritom privremeno sačuvaju (racuni_cijene_pdf)
    jer ih frontend ne vraća u POST-u; vidi `_cijene_za_spremanje`.
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
    nedostaje = missing_key_fields(parsed)
    if nedostaje:
        parsed['warning'] = (
            'Nisam pouzdano prepoznao: ' + ', '.join(nedostaje) +
            '. HEP je možda promijenio format računa — provjeri i ručno ispravi prije spremanja.'
        )
        log.warning('PDF %s: neprepoznata polja: %s', f.filename, nedostaje)
    _zapamti_cijene_pdf(parsed.get('period'), parsed.get('cijene'), parsed.get('broj_racuna'))
    return jsonify(parsed)
