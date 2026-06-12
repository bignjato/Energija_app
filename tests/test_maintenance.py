"""Testovi maintenance provjera (anomalija potrosnje, podsjetnik za racun)."""

import sqlite3
from datetime import datetime, timedelta

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
    assert notifikacije == []


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
