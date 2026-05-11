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
    }
    for k, v in defaults.items():
        conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (k, v))

    conn.commit()
    conn.close()


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
