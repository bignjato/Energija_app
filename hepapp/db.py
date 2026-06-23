"""SQLite helpers + schema init."""

import logging
import os
import sqlite3

from flask import current_app

from .core import hash_password

log = logging.getLogger(__name__)

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


# Trenutna verzija sheme. Povećaj kad dodaš migraciju u _run_migrations.
SCHEMA_VERSION = 1


def _get_schema_version(conn) -> int:
    conn.execute('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)')
    row = conn.execute('SELECT MAX(version) FROM schema_version').fetchone()
    return (row[0] if row and row[0] is not None else 0)


def _set_schema_version(conn, v: int):
    conn.execute('INSERT INTO schema_version (version) VALUES (?)', (v,))


def init_db():
    """Tablice, defaultovi, WAL mode."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated TEXT DEFAULT (datetime('now'))
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS korisnici (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            uloga TEXT DEFAULT 'viewer',
            aktivan INTEGER DEFAULT 1,
            stvoren TEXT DEFAULT (datetime('now')),
            zadnja_prijava TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS racuni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period TEXT NOT NULL UNIQUE,
            iznos REAL NOT NULL,
            kwh_plus REAL,
            kwh_minus REAL,
            kwh_vt REAL,
            kwh_nt REAL,
            opskrba REAL,
            mreza REAL,
            pdv REAL,
            napomena TEXT,
            stvoren TEXT DEFAULT (datetime('now'))
        )
    ''')

    # Verzionirane migracije (gate-ane schema_version tablicom).
    from_v = _get_schema_version(conn)
    _run_migrations(conn, from_v)

    count = conn.execute('SELECT COUNT(*) FROM korisnici').fetchone()[0]
    if count == 0:
        initial_pw = os.environ.get('INITIAL_ADMIN_PASSWORD', 'admin')
        conn.execute(
            'INSERT INTO korisnici (username, password_hash, uloga) VALUES (?, ?, ?)',
            ('admin', hash_password(initial_pw), 'admin'),
        )
        log.warning('Kreiran defaultni admin korisnik. PROMIJENI LOZINKU ODMAH.')

    migrated = conn.execute("SELECT value FROM config WHERE key='_migrated'").fetchone()
    if not migrated:
        env_keys = [
            'HEP_USERNAME', 'HEP_PASSWORD', 'HEP_SIFRA',
            'SMA_USERNAME', 'SMA_PASSWORD', 'SMA_CLIENT_ID',
            'SMA_PLANT_ID', 'SMA_INV1_ID', 'SMA_INV2_ID',
            'HA_URL', 'HA_TOKEN',
            'TARIFA_VT', 'TARIFA_NT', 'TARIFA_PROD',
            'TARIFA_PDV', 'TARIFA_VT_OD', 'TARIFA_VT_DO',
        ]
        for key in env_keys:
            val = os.environ.get(key, '')
            if val:
                conn.execute(
                    'INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (key, val)
                )
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('_migrated', '1')")
        log.info('Migriran .env u config tablicu')

    defaults = {
        'TARIFA_VT': '0.131205', 'TARIFA_NT': '0.064379',
        'TARIFA_PROD': '0.064379', 'TARIFA_PDV': '13',
        'TARIFA_VT_OD': '7', 'TARIFA_VT_DO': '21',
        'VT_UDIO_PERC': '45',
    }
    for k, v in defaults.items():
        conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (k, v))

    # Postojeće instalacije: ako imaju korisnike + HEP credentials → setup već gotov.
    setup_done = conn.execute("SELECT value FROM config WHERE key='_setup_complete'").fetchone()
    if not setup_done:
        has_users = conn.execute('SELECT COUNT(*) FROM korisnici').fetchone()[0] > 0
        has_hep   = bool(os.environ.get('HEP_USERNAME')) or bool(
            conn.execute("SELECT value FROM config WHERE key='HEP_USERNAME'").fetchone()
        )
        if has_users and has_hep:
            conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('_setup_complete', '1')")
            log.info('Postojeća instalacija — setup wizard preskačem.')

    # Datum tajni za check_token_age — postavi na migraciji ako fali
    if conn.execute("SELECT 1 FROM config WHERE key='_secrets_updated'").fetchone() is None:
        from datetime import date
        conn.execute("INSERT INTO config (key, value) VALUES ('_secrets_updated', ?)",
                     (date.today().isoformat(),))

    # Stamp current schema version once migrations completed
    if _get_schema_version(conn) < SCHEMA_VERSION:
        _set_schema_version(conn, SCHEMA_VERSION)
        log.info('Schema verzija → %d', SCHEMA_VERSION)

    conn.commit()
    conn.close()


def _run_migrations(conn, from_v: int):
    """Idempotentne, version-gate-ane migracije sheme.

    Svaka migracija mora biti sigurna za ponovno pokretanje. Za novu promjenu:
    povećaj SCHEMA_VERSION i dodaj blok `if from_v < N:`.
    """
    # v1: kolone HEP računa (dug, pretplata, datumi, model tarife, ...)
    if from_v < 1:
        racuni_cols = [
            ('dug', 'REAL'), ('pretplata', 'REAL'), ('mjerna_mjernina', 'REAL'),
            ('kamata', 'REAL'), ('datum_racuna', 'TEXT'), ('datum_dospijeca', 'TEXT'),
            ('model_tarife', 'TEXT'), ('sifra_kupca', 'TEXT'), ('broj_racuna', 'TEXT'),
        ]
        existing = {r[1] for r in conn.execute("PRAGMA table_info(racuni)").fetchall()}
        for col, typ in racuni_cols:
            if col not in existing:
                conn.execute(f'ALTER TABLE racuni ADD COLUMN {col} {typ}')
                log.info('Migracija v1: dodana racuni.%s', col)


def get_config(key, default=''):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
        conn.close()
        return row[0] if row else os.environ.get(key, default)
    except Exception:
        return os.environ.get(key, default)


def set_config(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT OR REPLACE INTO config (key, value, updated) VALUES (?, ?, datetime("now"))',
        (key, value),
    )
    conn.commit()
    conn.close()
