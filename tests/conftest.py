"""Zajednički setup: testovi se vrte u produkcijskoj zoni (TZ=Europe/Zagreb).

SQLite 'localtime'/'utc' modifikatori i datetime.now() ovise o TZ procesa,
pa ga fiksiramo prije importa aplikacije da suite bude deterministican.
"""

import os
import time

os.environ['TZ'] = 'Europe/Zagreb'
time.tzset()

import sqlite3

import pytest


@pytest.fixture
def prijavljeni_klijent(monkeypatch):
    """Vrati funkciju path -> test client prijavljen kao admin.

    Na zadanoj (već napunjenoj) bazi pokrene init_db() — tablica korisnici,
    migracije, indeksi — i otvori sesiju kakvu izdaje pravi login (otisak
    lozinke koji before_request provjerava prema bazi).
    """
    monkeypatch.setenv('SECRET_KEY', '0123456789abcdef0123456789abcdef')

    def _napravi(path):
        import hepapp.db as db
        monkeypatch.setattr(db, 'DB_PATH', path)
        from app import app
        import hepapp.core as core
        db.init_db()
        conn = sqlite3.connect(path)
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('_setup_complete','1')")
        pw_hash = conn.execute(
            "SELECT password_hash FROM korisnici WHERE username='admin'").fetchone()[0]
        conn.commit(); conn.close()

        app.config.update(TESTING=True)
        c = app.test_client()
        with c.session_transaction() as s:
            if hasattr(core, 'start_session'):
                with app.app_context():
                    core.start_session(s, 'admin', 'admin', pw_hash)
            else:
                s['logged_in'] = True
                s['uloga'] = 'admin'
        return c

    return _napravi
