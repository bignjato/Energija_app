"""Testovi maintenance provjera (anomalija, podsjetnik, backup, off-site, tokeni)."""

import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta

import pytest

import maintenance


@pytest.fixture
def conn():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT, kwh_plus REAL)')
    c.execute('CREATE TABLE racuni (period TEXT)')
    return c


@pytest.fixture
def notifikacije(monkeypatch):
    poslano = []
    monkeypatch.setattr(maintenance, 'ha_notify',
                        lambda title, msg: poslano.append((title, msg)) or True)
    return poslano


def _napuni_tjedne(conn, zadnji_kp):
    """8 prethodnih tjedana po 10 kWh na isti dan u tjednu + zadnji dan."""
    zadnji = datetime(2026, 6, 8)  # ponedjeljak
    for t in range(1, 9):
        d = (zadnji - timedelta(weeks=t)).strftime('%Y-%m-%d')
        conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 10.0)", (d,))
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, ?)",
                 (zadnji.strftime('%Y-%m-%d'), zadnji_kp))
    conn.commit()


def test_anomalija_iznad_praga(conn, notifikacije):
    _napuni_tjedne(conn, zadnji_kp=20.0)  # +100% vs prosjek 10
    assert maintenance.check_anomaly(conn, threshold_perc=50) is True
    assert len(notifikacije) == 1
    assert '2026-06-08' in notifikacije[0][1]


def test_normalna_potrosnja_bez_alarma(conn, notifikacije):
    _napuni_tjedne(conn, zadnji_kp=11.0)  # +10%
    assert maintenance.check_anomaly(conn, threshold_perc=50) is False
    assert notifikacije == []


def test_anomalija_samo_jednom_po_datumu(conn, notifikacije):
    _napuni_tjedne(conn, zadnji_kp=20.0)
    assert maintenance.check_anomaly(conn, threshold_perc=50) is True
    assert maintenance.check_anomaly(conn, threshold_perc=50) is False
    assert len(notifikacije) == 1


def test_premalo_referentnih_dana(conn, notifikacije):
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', '2026-06-08', 50.0)")
    conn.commit()
    assert maintenance.check_anomaly(conn, threshold_perc=50) is False


def test_sezonski_rast_bez_laznog_alarma(conn, notifikacije):
    """Same-weekday je nizak (ljeto), ali zadnjih 14 dana SVE poviseno (grijanje)
    => dan je visok vs isti-dan-u-tjednu, ali NE vs rolling-14 => bez alarma."""
    zadnji = datetime(2026, 11, 9)  # ponedjeljak
    for t in range(1, 9):  # ljetni isti-dan-u-tjednu ~10
        conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 10.0)",
                     ((zadnji - timedelta(weeks=t)).strftime('%Y-%m-%d'),))
    for dd in range(1, 14):  # zadnjih 13 dana svi ~20 (sezona)
        conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 20.0)",
                     ((zadnji - timedelta(days=dd)).strftime('%Y-%m-%d'),))
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 21.0)",
                 (zadnji.strftime('%Y-%m-%d'),))
    conn.commit()
    assert maintenance.check_anomaly(conn, threshold_perc=50) is False
    assert notifikacije == []


def test_pravi_skok_okida_alarm(conn, notifikacije):
    """I same-weekday i rolling-14 su ~10, zadnji dan 25 => probija oba okvira."""
    zadnji = datetime(2026, 11, 9)
    for t in range(1, 9):
        conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 10.0)",
                     ((zadnji - timedelta(weeks=t)).strftime('%Y-%m-%d'),))
    for dd in range(1, 14):
        conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 10.0)",
                     ((zadnji - timedelta(days=dd)).strftime('%Y-%m-%d'),))
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 25.0)",
                 (zadnji.strftime('%Y-%m-%d'),))
    conn.commit()
    assert maintenance.check_anomaly(conn, threshold_perc=50) is True
    assert len(notifikacije) == 1


def test_apsolutni_prag_filtrira_male_brojke(conn, notifikacije):
    """+75% ali apsolutno samo +1.5 kWh (< min_abs 3) => bez alarma."""
    _napuni_tjedne_v(conn, base=2.0, zadnji_kp=3.5)
    assert maintenance.check_anomaly(conn, threshold_perc=50) is False
    assert notifikacije == []


def _napuni_tjedne_v(conn, base, zadnji_kp):
    zadnji = datetime(2026, 6, 8)
    for t in range(1, 9):
        conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, ?)",
                     ((zadnji - timedelta(weeks=t)).strftime('%Y-%m-%d'), base))
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, ?)",
                 (zadnji.strftime('%Y-%m-%d'), zadnji_kp))
    conn.commit()


# ---------- restore_drill ----------
def _napravi_backup(dirpath, name, valid=True):
    path = os.path.join(dirpath, name)
    if valid:
        b = sqlite3.connect(path)
        b.executescript('''
            CREATE TABLE config (key TEXT, value TEXT);
            CREATE TABLE racuni (period TEXT);
            CREATE TABLE ocitanja_dnevna (datum TEXT, kwh_plus REAL);
            INSERT INTO ocitanja_dnevna VALUES ('2026-06-01', 5);
        ''')
        b.commit(); b.close()
    else:
        with open(path, 'wb') as f:
            f.write(b'ovo nije sqlite baza')
    return path


def test_restore_drill_ok(conn, notifikacije, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(tmp_path))
    _napravi_backup(str(tmp_path), 'hep_energy_20260620_0202.db')
    assert maintenance.restore_drill(conn) is True
    assert notifikacije == []


def test_restore_drill_nema_backupa(conn, notifikacije, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(tmp_path))
    assert maintenance.restore_drill(conn) is False
    assert len(notifikacije) == 1


def test_restore_drill_korumpiran(conn, notifikacije, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(tmp_path))
    _napravi_backup(str(tmp_path), 'hep_energy_20260620_0202.db', valid=False)
    assert maintenance.restore_drill(conn) is False
    assert len(notifikacije) == 1
    assert 'NEUSPJESAN' in notifikacije[0][0] or 'PROBLEM' in notifikacije[0][0]


def test_restore_drill_cadence(conn, notifikacije, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(tmp_path))
    _napravi_backup(str(tmp_path), 'hep_energy_20260620_0202.db')
    assert maintenance.restore_drill(conn) is True
    # drugi poziv isti dan => preskace (cadence)
    assert maintenance.restore_drill(conn) is False


# ---------- check_offsite ----------
def test_offsite_iskljucen_podsjeti(conn, notifikacije, monkeypatch):
    monkeypatch.setenv('OFFSITE_BACKUP_MODE', 'disabled')
    monkeypatch.delenv('OFFSITE_BACKUP_DEST', raising=False)
    assert maintenance.check_offsite(conn) is False
    assert len(notifikacije) == 1
    # drugi poziv => bez ponavljanja (remind cadence)
    assert maintenance.check_offsite(conn) is False
    assert len(notifikacije) == 1


def test_offsite_svjez_marker_ok(conn, notifikacije, monkeypatch, tmp_path):
    monkeypatch.setenv('OFFSITE_BACKUP_MODE', 'rclone')
    monkeypatch.setenv('OFFSITE_BACKUP_DEST', 'remote:hep/')
    marker = tmp_path / 'ok'
    marker.write_text(date.today().isoformat())
    monkeypatch.setattr(maintenance, 'OFFSITE_MARKER', str(marker))
    assert maintenance.check_offsite(conn) is False
    assert notifikacije == []


def test_offsite_zastario_marker_alarm(conn, notifikacije, monkeypatch, tmp_path):
    monkeypatch.setenv('OFFSITE_BACKUP_MODE', 'rclone')
    monkeypatch.setenv('OFFSITE_BACKUP_DEST', 'remote:hep/')
    marker = tmp_path / 'ok'
    marker.write_text((date.today() - timedelta(days=5)).isoformat())
    monkeypatch.setattr(maintenance, 'OFFSITE_MARKER', str(marker))
    assert maintenance.check_offsite(conn) is True
    assert len(notifikacije) == 1


# ---------- check_token_age ----------
def test_token_age_stare_tajne(conn, notifikacije):
    maintenance._set_cfg(conn, '_secrets_updated',
                         (date.today() - timedelta(days=120)).isoformat())
    assert maintenance.check_token_age(conn) is True
    assert len(notifikacije) == 1


def test_token_age_svjeze(conn, notifikacije):
    maintenance._set_cfg(conn, '_secrets_updated',
                         (date.today() - timedelta(days=10)).isoformat())
    assert maintenance.check_token_age(conn) is False
    assert notifikacije == []


def test_token_age_jednom_mjesecno(conn, notifikacije):
    maintenance._set_cfg(conn, '_secrets_updated',
                         (date.today() - timedelta(days=200)).isoformat())
    assert maintenance.check_token_age(conn) is True
    assert maintenance.check_token_age(conn) is False
    assert len(notifikacije) == 1


class _FiksniDatum(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 15)


def test_bill_reminder_salje_jednom(conn, notifikacije, monkeypatch):
    monkeypatch.setattr(maintenance, 'datetime', _FiksniDatum)
    assert maintenance.bill_reminder(conn) is True
    assert '05/2026' in notifikacije[0][1]
    # drugi poziv isti mjesec — bez ponavljanja
    assert maintenance.bill_reminder(conn) is False
    assert len(notifikacije) == 1


def test_bill_reminder_racun_postoji(conn, notifikacije, monkeypatch):
    monkeypatch.setattr(maintenance, 'datetime', _FiksniDatum)
    conn.execute("INSERT INTO racuni (period) VALUES ('05/2026')")
    conn.commit()
    assert maintenance.bill_reminder(conn) is False
    assert notifikacije == []


def test_bill_reminder_prerano_u_mjesecu(conn, notifikacije, monkeypatch):
    class Rano(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 5)
    monkeypatch.setattr(maintenance, 'datetime', Rano)
    assert maintenance.bill_reminder(conn) is False
