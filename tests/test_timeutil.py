"""Testovi za hepapp.timeutil: lokalno vrijeme, SQL "sada" izrazi, VT izraz."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from hepapp import timeutil
from hepapp.timeutil import (
    SQL_NOW_LOCAL, SQL_NOW_UTC, SQL_TODAY_LOCAL, now_local, raspon_mjeseca,
    sql_now_local, sql_today_local, today_local, utc_raspon_dana, vt_sql,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(':memory:')
    yield c
    c.close()


def _vt(conn, ts, **kw):
    """Evaluiraj vt_sql za jedan lokalni ts -> True/False."""
    return bool(conn.execute(f"SELECT {vt_sql('?', **kw)}", (ts,)).fetchone()[0])


# ---- lokalno "sada" ----

def test_now_local_je_aware_zagreb():
    n = now_local()
    assert n.tzinfo is not None
    assert n.utcoffset() in (timedelta(hours=1), timedelta(hours=2))
    assert today_local() == n.strftime('%Y-%m-%d') or today_local() == now_local().strftime('%Y-%m-%d')


def test_sql_now_local_format_i_vrijednost(conn):
    v = conn.execute(f'SELECT {SQL_NOW_LOCAL}').fetchone()[0]
    assert len(v) == 19 and v[10] == 'T'          # isti format kao HEP ts
    razlika = abs(datetime.fromisoformat(v) - now_local().replace(tzinfo=None))
    assert razlika < timedelta(seconds=5)


def test_sql_now_utc_format_i_vrijednost(conn):
    v = conn.execute(f'SELECT {SQL_NOW_UTC}').fetchone()[0]
    assert len(v) == 19 and v[10] == 'T'
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(datetime.fromisoformat(v) - utc) < timedelta(seconds=5)
    # prefiks sma_live formata: red "sada" (UTC, '+00:00') upada u [sada-1h, sada+1h]
    red = utc.strftime('%Y-%m-%dT%H:%M:%S') + '+00:00'
    assert conn.execute(
        f"SELECT ? >= {timeutil.sql_now_utc('-1 hours')} AND ? <= {timeutil.sql_now_utc('+1 hours')}",
        (red, red)).fetchone()[0] == 1


def test_sql_today_local(conn):
    prije = today_local()
    v = conn.execute(f'SELECT {SQL_TODAY_LOCAL}').fetchone()[0]
    assert v in (prije, today_local())            # tolerira prijelaz ponoći


def test_sql_modifikatori(conn):
    v = conn.execute(f"SELECT {sql_now_local('-7 days')}").fetchone()[0]
    ocekivano = now_local().replace(tzinfo=None) - timedelta(days=7)
    assert abs(datetime.fromisoformat(v) - ocekivano) < timedelta(seconds=5)
    d = conn.execute(f"SELECT {sql_today_local('-90 days')}").fetchone()[0]
    assert d == (now_local().replace(tzinfo=None) - timedelta(days=90)).strftime('%Y-%m-%d')


def test_danasnji_red_nije_u_buducnosti(conn):
    """Regresija: 'T' ts današnjeg dana leksički je > datetime('now') (razmak)."""
    ts = (now_local() - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S')
    assert conn.execute(f'SELECT ? <= {SQL_NOW_LOCAL}', (ts,)).fetchone()[0] == 1
    buduci = (now_local() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')
    assert conn.execute(f'SELECT ? <= {SQL_NOW_LOCAL}', (buduci,)).fetchone()[0] == 0


# ---- VT izraz: satni redovi (oznaka = početak sata) ----

def test_vt_satno_sijecanj(conn):
    """Zimsko računanje: VT 07-21 lokalno."""
    assert not _vt(conn, '2026-01-14T06:00:00')
    assert _vt(conn, '2026-01-14T07:00:00')
    assert _vt(conn, '2026-01-14T20:00:00')
    assert not _vt(conn, '2026-01-14T21:00:00')


def test_vt_satno_srpanj(conn):
    """Ljetno računanje: VT 08-22 lokalno."""
    assert not _vt(conn, '2026-07-15T07:00:00')
    assert _vt(conn, '2026-07-15T08:00:00')
    assert _vt(conn, '2026-07-15T21:00:00')
    assert not _vt(conn, '2026-07-15T22:00:00')


def test_vt_broj_sati_po_danu(conn):
    """Svaki dan ima točno 14 VT sati — i zimi i ljeti."""
    for dan in ('2026-01-14', '2026-07-15'):
        n = sum(_vt(conn, f'{dan}T{h:02d}:00:00') for h in range(24))
        assert n == 14, dan


def test_vt_konfigurabilni_sati(conn):
    # VT 06-22 zimski -> ljeti 07-23
    assert _vt(conn, '2026-01-14T06:00:00', vt_od=6, vt_do=22)
    assert not _vt(conn, '2026-01-14T22:00:00', vt_od=6, vt_do=22)
    assert not _vt(conn, '2026-07-15T06:00:00', vt_od=6, vt_do=22)
    assert _vt(conn, '2026-07-15T22:00:00', vt_od=6, vt_do=22)


# ---- VT izraz: 15-min uzorci (oznaka = KRAJ intervala) ----

def test_vt_15min_srpanj(conn):
    k = dict(kraj_intervala=True)
    assert not _vt(conn, '2026-07-15T08:00:00', **k)   # pokriva 07:45-08:00 -> NT
    assert _vt(conn, '2026-07-15T08:15:00', **k)       # pokriva 08:00-08:15 -> VT
    assert _vt(conn, '2026-07-15T22:00:00', **k)       # zadnji VT interval
    assert not _vt(conn, '2026-07-15T22:15:00', **k)


def test_vt_15min_sijecanj(conn):
    k = dict(kraj_intervala=True)
    assert not _vt(conn, '2026-01-14T07:00:00', **k)
    assert _vt(conn, '2026-01-14T07:15:00', **k)
    assert _vt(conn, '2026-01-14T21:00:00', **k)
    assert not _vt(conn, '2026-01-14T21:15:00', **k)


def test_vt_15min_ponoc_pripada_prethodnom_danu(conn):
    # 'D+1T00:00:00' je zadnji interval dana D (23:45-24:00) -> NT, bez greške
    assert not _vt(conn, '2026-07-16T00:00:00', kraj_intervala=True)


def test_vt_15min_broj_intervala(conn):
    """14 VT sati = 56 petnaestminutnih intervala po danu."""
    for dan in ('2026-01-14', '2026-07-15'):
        d0 = datetime.fromisoformat(dan)
        n = sum(_vt(conn, (d0 + timedelta(minutes=15 * i)).strftime('%Y-%m-%dT%H:%M:%S'),
                    kraj_intervala=True) for i in range(1, 97))
        assert n == 56, dan


# ---- dani promjene sata ----

@pytest.mark.parametrize('dan', ['2026-03-29', '2026-10-25'])
def test_vt_dst_dani_ne_pucaju(conn, dan):
    d0 = datetime.fromisoformat(dan)
    satni = [_vt(conn, f'{dan}T{h:02d}:00:00') for h in range(24)]
    # nepostojeći/dvostruki sat (02-03) je noću = NT; VT blok ostaje 14 sati
    assert not any(satni[:6])
    assert sum(satni) == 14
    kvart = [_vt(conn, (d0 + timedelta(minutes=15 * i)).strftime('%Y-%m-%dT%H:%M:%S'),
                 kraj_intervala=True) for i in range(1, 97)]
    assert sum(kvart) == 56


def test_vt_dst_nakon_promjene(conn):
    # 29.3.2026. je već ljetno računanje od 03:00 -> VT kreće u 08:00
    assert not _vt(conn, '2026-03-29T07:00:00')
    assert _vt(conn, '2026-03-29T08:00:00')
    # 25.10.2026. je od 03:00 zimsko -> VT kreće u 07:00
    assert _vt(conn, '2026-10-25T07:00:00')
    assert not _vt(conn, '2026-10-25T21:00:00')


# ---- rasponi ----

def test_raspon_mjeseca():
    assert raspon_mjeseca('2026-11') == ('2026-11-01', '2026-12-01')
    assert raspon_mjeseca('2026-12') == ('2026-12-01', '2027-01-01')
    for los in ('2026-13', '2026', 'abcdefg', '2026/07', ''):
        with pytest.raises(ValueError):
            raspon_mjeseca(los)


def test_utc_raspon_dana():
    assert utc_raspon_dana('2026-11-16') == ('2026-11-15T23:00:00', '2026-11-16T23:00:00')
    assert utc_raspon_dana('2026-07-15') == ('2026-07-14T22:00:00', '2026-07-15T22:00:00')
    assert utc_raspon_dana('2026-07-01', '2026-07-31') == ('2026-06-30T22:00:00', '2026-07-31T22:00:00')
    # dan prelaska na ljetno računanje traje 23 sata
    assert utc_raspon_dana('2026-03-29') == ('2026-03-28T23:00:00', '2026-03-29T22:00:00')
    with pytest.raises(ValueError):
        utc_raspon_dana('nije-datum')
    # rubovi raspona datetime-a: ValueError (ne OverflowError), da pozivatelj
    # s "except ValueError" padne na rezervni predikat
    for od, do in (('0001-01-01', '2026-11-16'), ('2026-01-01', '9999-12-31'),
                   ('9999-12-31', None)):
        with pytest.raises(ValueError):
            utc_raspon_dana(od, do)
