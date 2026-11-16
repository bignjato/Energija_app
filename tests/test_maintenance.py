"""Testovi maintenance provjera (anomalija, podsjetnik, backup, off-site, tokeni,
freshness state machine, backfill, izolacija koraka)."""

import json
import logging
import os
import sqlite3
import sys
import tempfile
import types
from datetime import date, datetime, timedelta, timezone

import pytest

import maintenance


@pytest.fixture
def conn():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT, kwh_plus REAL)')
    c.execute('CREATE TABLE ocitanja_15min (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL, '
              'UNIQUE(mjerno_mjesto, ts))')
    c.execute('CREATE TABLE racuni (period TEXT)')
    return c


@pytest.fixture
def bez_kanala(monkeypatch):
    """Oba kanala dolje: ha_notify vraca False."""
    pokusaji = []
    monkeypatch.setattr(maintenance, 'ha_notify',
                        lambda title, msg: pokusaji.append((title, msg)) and False)
    return pokusaji


def _puni_15min(conn, datum, n=96, omm='OMM1'):
    """n uzoraka dana `datum` po konvenciji kraja intervala: D 00:15 .. D+1 00:00."""
    d0 = datetime.fromisoformat(datum[:10])
    for i in range(1, n + 1):
        ts = (d0 + timedelta(minutes=15 * i)).strftime('%Y-%m-%dT%H:%M:%S')
        conn.execute('INSERT OR IGNORE INTO ocitanja_15min VALUES (?, ?, 0.1)', (omm, ts))
    conn.commit()


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
    _puni_15min(conn, zadnji.strftime('%Y-%m-%d'))


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
    _puni_15min(conn, '2026-06-08')
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
    _puni_15min(conn, zadnji.strftime('%Y-%m-%d'))
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
    _puni_15min(conn, zadnji.strftime('%Y-%m-%d'))
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
    _puni_15min(conn, zadnji.strftime('%Y-%m-%d'))


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


def test_bill_reminder_racun_u_iso_formatu(conn, notifikacije, monkeypatch):
    """racuni.period je u bazi 'YYYY-MM' (dashboard/racuni.py) — uvezen racun
    se mora prepoznati, inace podsjetnik stize svaki mjesec bez razloga."""
    monkeypatch.setattr(maintenance, 'datetime', _FiksniDatum)
    conn.execute("INSERT INTO racuni (period) VALUES ('2026-05')")
    conn.commit()
    assert maintenance.bill_reminder(conn) is False
    assert notifikacije == []


def test_bill_reminder_drugi_mjesec_ne_vrijedi(conn, notifikacije, monkeypatch):
    monkeypatch.setattr(maintenance, 'datetime', _FiksniDatum)
    conn.execute("INSERT INTO racuni (period) VALUES ('2026-04')")
    conn.commit()
    assert maintenance.bill_reminder(conn) is True
    assert '05/2026' in notifikacije[0][1]


def test_bill_reminder_prerano_u_mjesecu(conn, notifikacije, monkeypatch):
    class Rano(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 5)
    monkeypatch.setattr(maintenance, 'datetime', Rano)
    assert maintenance.bill_reminder(conn) is False


# ---------- check_anomaly: kompletnost dana ----------
def test_anomalija_nepotpun_dan_bez_markera(conn, notifikacije):
    """Zadnji dan djelomicno isporucen => preskoci BEZ markera; kad stigne ostatak, ocijeni."""
    zadnji = datetime(2026, 6, 8)
    for t in range(1, 9):
        conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 10.0)",
                     ((zadnji - timedelta(weeks=t)).strftime('%Y-%m-%d'),))
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', '2026-06-08', 20.0)")
    conn.commit()
    _puni_15min(conn, '2026-06-08', n=40)          # samo do 10:00
    assert maintenance.check_anomaly(conn, threshold_perc=50) is False
    assert notifikacije == []
    assert maintenance._get_cfg(conn, '_anomaly_last_date') == ''
    _puni_15min(conn, '2026-06-08', n=96)          # HEP isporucio ostatak
    assert maintenance.check_anomaly(conn, threshold_perc=50) is True
    assert maintenance._get_cfg(conn, '_anomaly_last_date') == '2026-06-08'


def test_anomalija_uzima_zadnji_kompletan_dan(conn, notifikacije):
    """Najnoviji red (danas, djelomican) ne smije sakriti jucerasnji kompletan dan."""
    _napuni_tjedne(conn, zadnji_kp=20.0)           # 2026-06-08 kompletan, +100%
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', '2026-06-09', 3.0)")
    conn.commit()
    _puni_15min(conn, '2026-06-09', n=30)
    assert maintenance.check_anomaly(conn, threshold_perc=50) is True
    assert '2026-06-08' in notifikacije[0][1]
    assert maintenance._get_cfg(conn, '_anomaly_last_date') == '2026-06-08'


@pytest.mark.parametrize('n,kompletan', [(91, False), (92, True), (96, True), (100, True)])
def test_dan_kompletan_granice_dst(conn, n, kompletan):
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', '2026-06-08', 5.0)")
    _puni_15min(conn, '2026-06-08', n=min(n, 96))
    for i in range(n - 96):                         # "dupli sat" (jesenski pomak)
        conn.execute("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, 0.1)",
                     (f'2026-06-08T02:{i:02d}:30',))
    conn.commit()
    assert maintenance._dan_kompletan(conn, '2026-06-08') is kompletan


def test_dan_kompletan_konvencija_kraja_intervala(conn):
    """96 uzoraka D 00:00..D 23:45 (oznaka POCETKA) NIJE cijeli dan D po konvenciji
    kraja intervala — fali D+1 00:00, a D 00:00 pripada danu D-1 => 95."""
    conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', '2026-06-08', 5.0)")
    d0 = datetime(2026, 6, 8)
    for i in range(96):
        conn.execute("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, 0.1)",
                     ((d0 + timedelta(minutes=15 * i)).strftime('%Y-%m-%dT%H:%M:%S'),))
    n = conn.execute("SELECT COUNT(*) FROM ocitanja_15min WHERE ts > '2026-06-08T00:00:00' "
                     "AND ts <= '2026-06-09T00:00:00'").fetchone()[0]
    assert n == 95
    assert maintenance._dan_kompletan(conn, '2026-06-08') is True   # 95 je unutar 92-100


# ---------- marker se postavlja tek nakon uspjesnog slanja ----------
def test_anomalija_marker_tek_nakon_slanja(conn, bez_kanala, monkeypatch):
    _napuni_tjedne(conn, zadnji_kp=20.0)
    assert maintenance.check_anomaly(conn, threshold_perc=50) is False
    assert len(bez_kanala) == 1
    assert maintenance._get_cfg(conn, '_anomaly_last_date') == ''
    # kanali opet rade => alert stize pri sljedecem pokretanju
    poslano = []
    monkeypatch.setattr(maintenance, 'ha_notify', lambda t, m: poslano.append(t) or True)
    assert maintenance.check_anomaly(conn, threshold_perc=50) is True
    assert len(poslano) == 1
    assert maintenance._get_cfg(conn, '_anomaly_last_date') == '2026-06-08'


def test_bill_reminder_marker_tek_nakon_slanja(conn, bez_kanala, monkeypatch):
    monkeypatch.setattr(maintenance, 'datetime', _FiksniDatum)
    assert maintenance.bill_reminder(conn) is False
    assert len(bez_kanala) == 1
    assert maintenance._get_cfg(conn, '_bill_reminder_last') == ''
    assert maintenance.bill_reminder(conn) is False
    assert len(bez_kanala) == 2                    # pokusava ponovno


def test_token_age_marker_tek_nakon_slanja(conn, bez_kanala):
    maintenance._set_cfg(conn, '_secrets_updated',
                         (date.today() - timedelta(days=200)).isoformat())
    assert maintenance.check_token_age(conn) is False
    assert maintenance._get_cfg(conn, '_token_age_warn_month') == ''
    assert maintenance.check_token_age(conn) is False
    assert len(bez_kanala) == 2


def test_offsite_marker_tek_nakon_slanja(conn, bez_kanala, monkeypatch):
    monkeypatch.setenv('OFFSITE_BACKUP_MODE', 'disabled')
    monkeypatch.delenv('OFFSITE_BACKUP_DEST', raising=False)
    maintenance.check_offsite(conn)
    assert maintenance._get_cfg(conn, '_offsite_warn_last') == ''
    maintenance.check_offsite(conn)
    assert len(bez_kanala) == 2


def test_restore_drill_marker_tek_nakon_slanja(conn, bez_kanala, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(tmp_path))
    _napravi_backup(str(tmp_path), 'hep_energy_20260620_0202.db', valid=False)
    assert maintenance.restore_drill(conn) is False
    assert maintenance._get_cfg(conn, '_restore_drill_last') == ''
    assert maintenance.restore_drill(conn) is False   # nema cadence preskakanja
    assert len(bez_kanala) == 2


# ---------- _tg_send / ha_notify ----------
def test_tg_send_ne_logira_token(monkeypatch, caplog):
    import requests
    tajna = '123456:TAJNI-BOT-TOKEN'
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', tajna)
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '42')

    def post(url, **kw):
        raise requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: {url}")
    monkeypatch.setattr(requests, 'post', post)
    with caplog.at_level(logging.DEBUG, logger='maintenance'):
        assert maintenance._tg_send('naslov', 'poruka') is False
    assert 'ConnectionError' in caplog.text
    assert tajna not in caplog.text
    assert 'api.telegram.org' not in caplog.text


def test_tg_send_logira_http_status(monkeypatch, caplog):
    import requests
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123456:TAJNA')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '42')

    def post(url, **kw):
        resp = requests.Response()
        resp.status_code = 502
        raise requests.exceptions.HTTPError(f'502 Bad Gateway for url: {url}', response=resp)
    monkeypatch.setattr(requests, 'post', post)
    with caplog.at_level(logging.DEBUG, logger='maintenance'):
        assert maintenance._tg_send('naslov', 'poruka') is False
    assert 'HTTP 502' in caplog.text
    assert '123456:TAJNA' not in caplog.text


class _TgOdgovor:
    def __init__(self, status):
        self.status_code = status
        self.ok = status < 400
        self.text = '{"ok":false,"description":"Bad Request: can\'t parse entities"}'


def _tg_lazni(monkeypatch, strogi_markdown=True):
    """Lazni Telegram: kao pravi API odbija (400) Markdown s neuparenim '_'."""
    import re
    import requests
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123456:TAJNA')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '42')
    pozivi = []

    def post(url, json=None, **kw):
        pozivi.append(json)
        if json.get('parse_mode') == 'Markdown':
            goli = re.sub(r'\\.', '', json['text'])          # bez escapeanih znakova
            if not strogi_markdown or goli.count('_') % 2 or goli.count('*') % 2:
                return _TgOdgovor(400)
        return _TgOdgovor(200)
    monkeypatch.setattr(requests, 'post', post)
    return pozivi


def test_tg_send_escapea_markdown_znakove(monkeypatch):
    """Neuparen '_' (ime tablice/backup datoteke) ne smije srusiti slanje."""
    pozivi = _tg_lazni(monkeypatch)
    assert maintenance._tg_send('Energetski Monitor — backup',
                                'Restore drill NEUSPJESAN (hep_energy_20261115.db): '
                                'nedostaje tablica ocitanja_dnevna') is True
    assert len(pozivi) == 1 and pozivi[0]['parse_mode'] == 'Markdown'
    assert 'hep\\_energy\\_20261115.db' in pozivi[0]['text']
    assert pozivi[0]['text'].startswith('\u26a1 *Energetski Monitor — backup*\n')


def test_tg_send_naslov_bez_markdown_znakova(monkeypatch):
    pozivi = _tg_lazni(monkeypatch)
    assert maintenance._tg_send('a_b*c', 'poruka') is True
    assert pozivi[0]['text'].startswith('\u26a1 *abc*\n')


def test_tg_send_fallback_na_obican_tekst(monkeypatch):
    """Telegram odbije Markdown (400) => jos jedan pokusaj bez parse_mode."""
    pozivi = _tg_lazni(monkeypatch, strogi_markdown=False)
    assert maintenance._tg_send('naslov', 'tablica sma_live') is True
    assert len(pozivi) == 2
    assert 'parse_mode' not in pozivi[1]
    assert pozivi[1]['text'] == '\u26a1 naslov\ntablica sma_live'


def test_tg_send_ne_ponavlja_na_druge_greske(monkeypatch):
    import requests
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123456:TAJNA')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '42')
    pozivi = []
    monkeypatch.setattr(requests, 'post',
                        lambda url, **kw: pozivi.append(kw) or _TgOdgovor(401))
    assert maintenance._tg_send('naslov', 'poruka') is False
    assert len(pozivi) == 1


def test_freshness_poruke_prolaze_telegram_markdown(fresh, sat, monkeypatch):
    """Stvarne poruke check_freshness (jedna prazna tablica) kroz _tg_send."""
    pozivi = _tg_lazni(monkeypatch)
    monkeypatch.setattr(maintenance, '_ha_send', lambda t, m: False)
    fresh.execute('DELETE FROM sma_live')
    fresh.commit()
    assert maintenance.check_freshness(fresh) is True
    assert len(pozivi) == 1                      # proslo iz prve, bez fallbacka
    assert _stanje(fresh)['problemi'] == ['sma']


def test_ha_notify_true_ako_barem_jedan_kanal(monkeypatch):
    monkeypatch.setattr(maintenance, '_ha_send', lambda t, m: False)
    monkeypatch.setattr(maintenance, '_tg_send', lambda t, m: True)
    assert maintenance.ha_notify('a', 'b') is True
    monkeypatch.setattr(maintenance, '_tg_send', lambda t, m: False)
    assert maintenance.ha_notify('a', 'b') is False


# ---------- backfill_range / backfill_daily ----------
@pytest.fixture
def sma():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.executescript('''
        CREATE TABLE sma_live (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            pv_generation_w REAL, feed_in_w REAL, external_consumption_w REAL,
            total_consumption_w REAL, direct_consumption_w REAL,
            autarky_rate REAL, self_consumption_rate REAL, battery_soc REAL);
        CREATE TABLE sma_15min (
            ts TEXT PRIMARY KEY, pv_generation_wh REAL, feed_in_wh REAL,
            grid_consumption_wh REAL, total_consumption_wh REAL,
            pv_w_inv1 REAL DEFAULT 0, pv_w_inv2 REAL DEFAULT 0, pv_w_total REAL DEFAULT 0);
        CREATE TABLE sma_dnevna (
            datum TEXT PRIMARY KEY, pv_generation_kwh REAL, feed_in_kwh REAL,
            grid_consumption_kwh REAL, total_consumption_kwh REAL,
            self_consumption_kwh REAL, autarky_rate REAL,
            pv_kwh_inv1 REAL, pv_kwh_inv2 REAL);
    ''')
    return c


def _live(c, ts, feed=1200.0, pv=2000.0, grid=0.0, cons=800.0):
    c.execute('INSERT INTO sma_live (ts, pv_generation_w, feed_in_w, external_consumption_w, '
              'total_consumption_w) VALUES (?,?,?,?,?)', (ts, pv, feed, grid, cons))


def test_backfill_range_upsert_cuva_ostale_stupce(sma, monkeypatch):
    sma.execute("INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh, "
                "total_consumption_kwh, autarky_rate, pv_kwh_inv1, pv_kwh_inv2) "
                "VALUES ('2026-06-10', 1.0, 11.1, 2.2, 33.3, 0.93, 0.5, 0.5)")
    sma.commit()
    pozivi = []

    def fetch_week(token, od, do):
        pozivi.append((od, do))
        return ({'2026-06-10T10:00:00Z': 4000.0, '2026-06-10T10:15:00Z': 4000.0},
                {'2026-06-10T10:00:00Z': 2000.0})
    lazni = types.ModuleType('sma_history_import')
    lazni.get_token = lambda: 'x'
    lazni.fetch_week = fetch_week
    monkeypatch.setitem(sys.modules, 'sma_history_import', lazni)

    assert maintenance.backfill_range(sma, '2026-06-10', '2026-06-10') == 2
    r = sma.execute("SELECT * FROM sma_dnevna WHERE datum='2026-06-10'").fetchone()
    # PV stupci osvjezeni: (6000 + 4000) W * 0.25 h = 2.5 kWh
    assert r['pv_generation_kwh'] == pytest.approx(2.5)
    assert r['pv_kwh_inv1'] == pytest.approx(2.0)
    assert r['pv_kwh_inv2'] == pytest.approx(0.5)
    # flow stupci netaknuti (INSERT OR REPLACE ih je brisao)
    assert r['feed_in_kwh'] == 11.1
    assert r['grid_consumption_kwh'] == 2.2
    assert r['total_consumption_kwh'] == 33.3
    assert r['autarky_rate'] == 0.93


def test_agregacija_pv_novi_dan_i_lokalni_datum(sma):
    # 22:30Z ljeti = 00:30 lokalno SLJEDECEG dana
    sma.execute("INSERT INTO sma_15min (ts, pv_w_inv1, pv_w_inv2, pv_w_total) "
                "VALUES ('2026-06-10T22:30:00Z', 400, 400, 800)")
    maintenance._agregiraj_pv_dnevno(sma, '2026-06-10', '2026-06-11')
    dani = [r['datum'] for r in sma.execute('SELECT datum FROM sma_dnevna')]
    assert dani == ['2026-06-11']


def test_backfill_daily_lokalni_dan_oko_ponoci(sma):
    # ljeto: lokalno = UTC+2 => 21:50Z/21:55Z su 23:50/23:55 10.6., 22:05Z/22:10Z su 00:05/00:10 11.6.
    for ts in ('2026-06-10T21:50:00+00:00', '2026-06-10T21:55:00+00:00',
               '2026-06-10T22:05:00+00:00', '2026-06-10T22:10:00+00:00'):
        _live(sma, ts)
    sma.commit()
    assert maintenance.backfill_daily(sma) == 2
    r = {x['datum']: x['feed_in_kwh'] for x in sma.execute('SELECT * FROM sma_dnevna')}
    # 1200 W * 5 min = 0.1 kWh po danu; UTC bucketing bi sve stavio na 10.6.
    assert r == {'2026-06-10': pytest.approx(0.1), '2026-06-11': pytest.approx(0.1)}


def test_backfill_daily_zimi_utc_plus_1(sma):
    for ts in ('2026-01-10T22:50:00+00:00', '2026-01-10T22:55:00+00:00',   # 23:50 lokalno 10.1.
               '2026-01-10T23:05:00+00:00', '2026-01-10T23:10:00+00:00'):  # 00:05 lokalno 11.1.
        _live(sma, ts)
    sma.commit()
    maintenance.backfill_daily(sma)
    assert sorted(x['datum'] for x in sma.execute('SELECT datum FROM sma_dnevna')) == \
        ['2026-01-10', '2026-01-11']


def test_backfill_daily_naivni_ts_kao_utc(sma):
    # bez offseta => UTC (ne odbacuj): 22:05/22:10 UTC = 11.6. lokalno
    _live(sma, '2026-06-10T22:05:00')
    _live(sma, '2026-06-10T22:10:00')
    sma.commit()
    assert maintenance.backfill_daily(sma) == 1
    r = sma.execute('SELECT * FROM sma_dnevna').fetchone()
    assert r['datum'] == '2026-06-11'
    assert r['feed_in_kwh'] == pytest.approx(0.1)


def test_backfill_daily_preskace_danas(sma):
    sada = datetime.now(timezone.utc)
    for m in (10, 5, 0):
        _live(sma, (sada - timedelta(minutes=m)).strftime('%Y-%m-%dT%H:%M:%S+00:00'))
    sma.commit()
    maintenance.backfill_daily(sma)
    danas = datetime.now().strftime('%Y-%m-%d')        # TZ=Europe/Zagreb => lokalni dan
    assert sma.execute('SELECT 1 FROM sma_dnevna WHERE datum=?', (danas,)).fetchone() is None


def test_backfill_daily_ne_gazi_postojece(sma):
    sma.execute("INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh) VALUES ('2026-06-10', 30.0, 7.7)")
    sma.execute("INSERT INTO sma_dnevna (datum, pv_generation_kwh) VALUES ('2026-06-12', 25.0)")
    for d in ('2026-06-10', '2026-06-12'):
        _live(sma, f'{d}T10:00:00+00:00')
        _live(sma, f'{d}T10:05:00+00:00')
    sma.commit()
    assert maintenance.backfill_daily(sma) == 1       # samo dan bez feed_in_kwh
    r = {x['datum']: x for x in sma.execute('SELECT * FROM sma_dnevna')}
    assert r['2026-06-10']['feed_in_kwh'] == 7.7
    assert r['2026-06-12']['feed_in_kwh'] == pytest.approx(0.1)
    assert r['2026-06-12']['pv_generation_kwh'] == 25.0   # PV ostaje iz importa


# ---------- main(): izolacija koraka ----------
def _lazni_conn(monkeypatch):
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    monkeypatch.setattr(maintenance, '_conn', lambda: c)
    return c


def test_main_izolira_korake(monkeypatch, caplog):
    _lazni_conn(monkeypatch)
    pozvano = []

    def pukni(conn):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(maintenance, 'backfill_daily', pukni)
    monkeypatch.setattr(maintenance, 'check_freshness', lambda c: pozvano.append('freshness'))
    monkeypatch.setattr(maintenance, 'check_anomaly', lambda c: pozvano.append('anomaly'))
    with caplog.at_level(logging.ERROR, logger='maintenance'):
        rc = maintenance.main(['--backfill-daily', '--check-freshness', '--check-anomaly'])
    assert rc == 1
    assert pozvano == ['freshness', 'anomaly']
    assert 'backfill-daily' in caplog.text and 'database is locked' in caplog.text


def test_main_sve_ok_vraca_0(monkeypatch):
    _lazni_conn(monkeypatch)
    pozvano = []
    monkeypatch.setattr(maintenance, 'prune_live', lambda c, k: pozvano.append(('prune', k)))
    monkeypatch.setattr(maintenance, 'check_inverter_balance', lambda c: pozvano.append('inv'))
    assert maintenance.main(['--prune-live', '--keep-days', '45', '--check-inverter']) == 0
    assert pozvano == [('prune', 45), 'inv']


def test_main_greska_ne_logira_url(monkeypatch, caplog):
    _lazni_conn(monkeypatch)

    def pukni(conn):
        raise RuntimeError('GET https://example.invalid/api?token=TAJNA failed')
    monkeypatch.setattr(maintenance, 'check_freshness', pukni)
    with caplog.at_level(logging.ERROR, logger='maintenance'):
        assert maintenance.main(['--check-freshness']) == 1
    assert 'RuntimeError' in caplog.text and 'TAJNA' not in caplog.text


def test_conn_ima_busy_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, 'DB_PATH', str(tmp_path / 't.db'))
    c = maintenance._conn()
    try:
        assert c.execute('PRAGMA busy_timeout').fetchone()[0] == 30000
    finally:
        c.close()


# ---------- prune_live ----------
def test_prune_live_granica_istog_formata(sma):
    sada = datetime.now(timezone.utc)
    f = '%Y-%m-%dT%H:%M:%S+00:00'
    _live(sma, (sada - timedelta(days=30, hours=1)).strftime(f))   # stariji od granice
    _live(sma, (sada - timedelta(days=29, hours=23)).strftime(f))  # mladji
    sma.commit()
    assert maintenance.prune_live(sma, keep_days=30) == 1
    assert sma.execute('SELECT COUNT(*) FROM sma_live').fetchone()[0] == 1


# ---------- check_freshness: state machine ----------
class _Sat:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def naprijed(self, sati):
        self.t += sati * 3600


@pytest.fixture
def sat(monkeypatch):
    s = _Sat()
    monkeypatch.setattr(maintenance, '_now_ts', s)
    return s


@pytest.fixture
def fresh():
    """Zdravo stanje: HEP od jucer, SMA uzorak upravo sada, zadnji sync OK."""
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.executescript('''
        CREATE TABLE ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT, kwh_plus REAL);
        CREATE TABLE ocitanja_15min (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL,
            kwh_minus REAL, UNIQUE(mjerno_mjesto, ts));
        CREATE TABLE sma_live (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL);
        CREATE TABLE sync_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT (datetime('now')), tip TEXT, status TEXT, poruka TEXT,
            zapisi INTEGER DEFAULT 0);
    ''')
    _hep(c, 1)
    _sma_ts(c, 0)
    c.execute("INSERT INTO sync_log (tip, status) VALUES ('sync', 'OK')")
    c.commit()
    return c


def _hep(c, dana_unazad):
    """Zadnji KOMPLETAN HEP dan je prije `dana_unazad` dana: dnevni red + zadnji
    15-min uzorak tog dana (oznaka kraja intervala = iduci dan 00:00:00)."""
    d = date.today() - timedelta(days=dana_unazad)
    c.execute('DELETE FROM ocitanja_dnevna')
    c.execute('DELETE FROM ocitanja_15min')
    c.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 5.0)", (d.isoformat(),))
    c.execute("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, 0.1, 0)",
              ((d + timedelta(days=1)).isoformat() + 'T00:00:00',))
    c.commit()


def _hep_uzorak(c, sati_unazad, kp=0.1, km=0.0, brisi=True):
    """15-min uzorak star `sati_unazad` h (lokalno naivno vrijeme)."""
    if brisi:
        c.execute('DELETE FROM ocitanja_dnevna')
        c.execute('DELETE FROM ocitanja_15min')
    ts = (datetime.now() - timedelta(hours=sati_unazad)).strftime('%Y-%m-%dT%H:%M:%S')
    c.execute("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, ?, ?)", (ts, kp, km))
    c.commit()


def _sma_ts(c, sati_unazad, fmt='%Y-%m-%dT%H:%M:%S+00:00'):
    c.execute('DELETE FROM sma_live')
    c.execute('INSERT INTO sma_live (ts) VALUES (?)',
              ((datetime.now(timezone.utc) - timedelta(hours=sati_unazad)).strftime(fmt),))
    c.commit()


def _stanje(c):
    return json.loads(maintenance._get_cfg(c, maintenance.FRESHNESS_STATE_KEY) or '{}')


def test_freshness_zdravo_bez_poruke(fresh, notifikacije, sat):
    assert maintenance.check_freshness(fresh) is False
    assert notifikacije == []


def test_freshness_hep_nocni_zaostatak_nije_problem(fresh, notifikacije, sat):
    """HEP dan D isporucuje tek tijekom D+1: od ponoci do isporuke zadnji
    kompletan dan je prekjucerasnji. To je normalno i NE smije okinuti alert
    (inace alert iza ponoci + "rijeseno" svako jutro)."""
    _hep(fresh, 1)
    assert maintenance.check_freshness(fresh) is False
    _hep(fresh, 2)
    assert maintenance.check_freshness(fresh) is False
    assert notifikacije == []
    _hep(fresh, 3)                              # cijeli dan bez isporuke => alert
    assert maintenance.check_freshness(fresh) is True
    assert len(notifikacije) == 1 and 'HEP' in notifikacije[0][1]


def test_freshness_hep_prag_u_satima(fresh, notifikacije, sat):
    _hep_uzorak(fresh, 47)
    assert maintenance.check_freshness(fresh) is False
    _hep_uzorak(fresh, 49)
    assert maintenance.check_freshness(fresh) is True
    assert 'HEP podaci kasne 49 h' in notifikacije[0][1]


def test_freshness_max_hep_h_se_postuje(fresh, notifikacije, sat):
    _hep_uzorak(fresh, 30)
    assert maintenance.check_freshness(fresh) is False
    assert maintenance.check_freshness(fresh, max_hep_h=24) is True


def test_freshness_hep_nula_uzorci_se_ne_broje(fresh, notifikacije, sat):
    """HEP-ova nula-popuna za jos neisporuceno razdoblje nije svjez podatak."""
    _hep_uzorak(fresh, 60)
    _hep_uzorak(fresh, 1, kp=0.0, km=0.0, brisi=False)
    assert maintenance.check_freshness(fresh) is True
    # uzorak samo s predajom u mrezu (kwh_minus) JE stvaran podatak
    _hep_uzorak(fresh, 2, kp=0.0, km=0.3, brisi=False)
    assert maintenance.check_freshness(fresh) is False


def test_freshness_hep_bez_15min_koristi_dnevnu(fresh, notifikacije, sat):
    fresh.execute('DELETE FROM ocitanja_15min')
    fresh.commit()                              # ostaje dnevni red od jucer
    assert maintenance.check_freshness(fresh) is False
    fresh.execute('DROP TABLE ocitanja_15min')
    fresh.execute("UPDATE ocitanja_dnevna SET datum = ?",
                  ((date.today() - timedelta(days=3)).isoformat(),))
    fresh.commit()
    assert maintenance.check_freshness(fresh) is True
    assert 'HEP' in notifikacije[0][1]


def test_freshness_prvi_alert_pa_bez_ponavljanja_24h(fresh, notifikacije, sat):
    _hep(fresh, 3)
    assert maintenance.check_freshness(fresh) is True
    assert len(notifikacije) == 1
    assert _stanje(fresh)['problemi'] == ['hep']
    for _ in range(23):                         # svaki sat, 23 h
        sat.naprijed(1)
        assert maintenance.check_freshness(fresh) is True
    assert len(notifikacije) == 1


def test_freshness_ponavlja_nakon_24h(fresh, notifikacije, sat):
    _hep(fresh, 3)
    maintenance.check_freshness(fresh)
    sat.naprijed(24)
    assert maintenance.check_freshness(fresh) is True
    assert len(notifikacije) == 2
    sat.naprijed(1)
    maintenance.check_freshness(fresh)
    assert len(notifikacije) == 2               # brojac od zadnje poruke


def test_freshness_novi_problem_javlja_odmah(fresh, notifikacije, sat):
    _hep(fresh, 3)
    maintenance.check_freshness(fresh)
    sat.naprijed(1)
    _sma_ts(fresh, 13)
    assert maintenance.check_freshness(fresh) is True
    assert len(notifikacije) == 2
    assert 'HEP' in notifikacije[1][1] and 'SMA' in notifikacije[1][1]
    assert _stanje(fresh)['problemi'] == ['hep', 'sma']


def test_freshness_rijeseno_jedna_poruka(fresh, notifikacije, sat):
    _hep(fresh, 3)
    maintenance.check_freshness(fresh)
    sat.naprijed(2)
    _hep(fresh, 1)
    assert maintenance.check_freshness(fresh) is False
    assert len(notifikacije) == 2
    assert 'rijeseno' in notifikacije[1][0]
    sat.naprijed(1)
    assert maintenance.check_freshness(fresh) is False
    assert len(notifikacije) == 2               # "rijeseno" samo jednom
    # novi ispad nakon oporavka => opet alert odmah
    _hep(fresh, 3)
    assert maintenance.check_freshness(fresh) is True
    assert len(notifikacije) == 3


def test_freshness_djelomicno_rijeseno_pa_povratak(fresh, notifikacije, sat):
    _hep(fresh, 3)
    _sma_ts(fresh, 13)
    maintenance.check_freshness(fresh)
    assert len(notifikacije) == 1
    sat.naprijed(1)
    _sma_ts(fresh, 0)                           # SMA ok, HEP i dalje kasni
    maintenance.check_freshness(fresh)
    assert len(notifikacije) == 1               # bez poruke, nije sve rijeseno
    sat.naprijed(1)
    _sma_ts(fresh, 13)                          # SMA opet pao => novi problem
    maintenance.check_freshness(fresh)
    assert len(notifikacije) == 2


def test_freshness_stanje_tek_nakon_uspjesnog_slanja(fresh, bez_kanala, sat, monkeypatch):
    _hep(fresh, 3)
    assert maintenance.check_freshness(fresh) is True
    assert _stanje(fresh) == {}
    sat.naprijed(1)
    maintenance.check_freshness(fresh)
    assert len(bez_kanala) == 2                 # pokusava svaki put dok ne uspije
    poslano = []
    monkeypatch.setattr(maintenance, 'ha_notify', lambda t, m: poslano.append(t) or True)
    maintenance.check_freshness(fresh)
    assert len(poslano) == 1 and _stanje(fresh)['problemi'] == ['hep']
    # "rijeseno" koje nije isporuceno se ponavlja
    _hep(fresh, 1)
    monkeypatch.setattr(maintenance, 'ha_notify', lambda t, m: False)
    maintenance.check_freshness(fresh)
    assert _stanje(fresh)['problemi'] == ['hep']
    monkeypatch.setattr(maintenance, 'ha_notify', lambda t, m: poslano.append(t) or True)
    maintenance.check_freshness(fresh)
    assert len(poslano) == 2 and _stanje(fresh)['problemi'] == []


def test_freshness_naivni_sma_ts_kao_utc(fresh, notifikacije, sat):
    _sma_ts(fresh, 13, fmt='%Y-%m-%dT%H:%M:%S')     # bez offseta, star 13 h
    assert maintenance.check_freshness(fresh) is True
    assert 'SMA' in notifikacije[0][1]


def test_freshness_naivni_svjezi_sma_ts_nije_problem(fresh, notifikacije, sat):
    _sma_ts(fresh, 1, fmt='%Y-%m-%dT%H:%M:%S')
    assert maintenance.check_freshness(fresh) is False
    assert notifikacije == []


def test_freshness_max_sma_h_se_postuje(fresh, notifikacije, sat):
    _sma_ts(fresh, 5)
    assert maintenance.check_freshness(fresh) is False
    assert maintenance.check_freshness(fresh, max_sma_h=4) is True


def test_freshness_prazne_tablice(fresh, notifikacije, sat):
    fresh.execute('DELETE FROM ocitanja_dnevna')
    fresh.execute('DELETE FROM ocitanja_15min')
    fresh.execute('DELETE FROM sma_live')
    fresh.commit()
    assert maintenance.check_freshness(fresh) is True
    assert len(notifikacije) == 1
    assert 'HEP podaci: nema nijednog' in notifikacije[0][1]
    assert 'SMA live: nema nijednog' in notifikacije[0][1]
    assert '_' not in notifikacije[0][1]         # bez imena tablica (Telegram Markdown)
    assert _stanje(fresh)['problemi'] == ['hep', 'sma']


def test_freshness_oporavljeni_ispad_bez_alarma(fresh, notifikacije, sat):
    """30 gresaka prije par sati, ali sync opet prolazi => bez alarma (stari
    '>=20 gresaka u 24 h' je ovdje lazno okidao jos 24 h)."""
    fresh.execute('DELETE FROM sync_log')
    for _ in range(30):
        fresh.execute("INSERT INTO sync_log (ts, tip, status) VALUES (datetime('now','-3 hours'), 'login', 'ERROR')")
    fresh.execute("INSERT INTO sync_log (ts, tip, status) VALUES (datetime('now','-1 hours'), 'sync', 'OK')")
    fresh.commit()
    assert maintenance.check_freshness(fresh) is False
    assert notifikacije == []


def test_freshness_sync_pada_sada(fresh, notifikacije, sat):
    fresh.execute('DELETE FROM sync_log')
    fresh.execute("INSERT INTO sync_log (ts, tip, status) VALUES (datetime('now','-7 hours'), 'sync', 'OK')")
    for h in range(6, 0, -1):
        fresh.execute("INSERT INTO sync_log (ts, tip, status) VALUES (datetime('now', ?), 'login', 'ERROR')",
                      (f'-{h} hours',))
    fresh.commit()
    assert maintenance.check_freshness(fresh) is True
    assert 'sync' in notifikacije[0][1] and '6 gresaka' in notifikacije[0][1]
    assert _stanje(fresh)['problemi'] == ['sync']


def test_freshness_sync_greske_bez_ijednog_ok(fresh, notifikacije, sat):
    fresh.execute('DELETE FROM sync_log')
    fresh.execute("INSERT INTO sync_log (tip, status) VALUES ('login', 'ERROR')")
    fresh.commit()
    assert maintenance.check_freshness(fresh) is True
    assert 'sync' in notifikacije[0][1]


def test_freshness_stari_ok_bez_gresaka_nije_sync_problem(fresh, notifikacije, sat):
    fresh.execute('DELETE FROM sync_log')
    fresh.execute("INSERT INTO sync_log (ts, tip, status) VALUES (datetime('now','-9 hours'), 'sync', 'OK')")
    fresh.commit()
    assert maintenance.check_freshness(fresh) is False


def test_freshness_pokvareno_stanje_u_configu(fresh, notifikacije, sat):
    maintenance._set_cfg(fresh, maintenance.FRESHNESS_STATE_KEY, 'nije-json')
    _hep(fresh, 3)
    assert maintenance.check_freshness(fresh) is True
    assert len(notifikacije) == 1


# ---------- check_inverter_balance ----------
def _inv_povijest(c, dana=15, i1=20.0, i2=20.0, kraj=2):
    """Zdravi dani (oba invertera) koji zavrsavaju `kraj` dana prije danas."""
    for d in range(kraj, kraj + dana):
        c.execute('INSERT INTO sma_dnevna (datum, pv_generation_kwh, pv_kwh_inv1, pv_kwh_inv2) VALUES (?,?,?,?)',
                  ((date.today() - timedelta(days=d)).isoformat(), i1 + i2, i1, i2))
    c.commit()


def _inv_jucer(c, i1, i2):
    c.execute('INSERT INTO sma_dnevna (datum, pv_kwh_inv1, pv_kwh_inv2) VALUES (?,?,?)',
              ((date.today() - timedelta(days=1)).isoformat(), i1, i2))
    c.commit()


def test_inverter_zdrav_bez_alarma(sma, notifikacije):
    _inv_povijest(sma)
    _inv_jucer(sma, 21.0, 20.0)
    assert maintenance.check_inverter_balance(sma, threshold_pp=8, min_day_kwh=20) is False
    assert notifikacije == []


@pytest.mark.parametrize('i1,i2,losi', [(0.0, 25.0, 'INV1'), (25.0, None, 'INV2')])
def test_inverter_mrtav_okida_alarm(sma, notifikacije, i1, i2, losi):
    _inv_povijest(sma)
    _inv_jucer(sma, i1, i2)                     # jedan inverter 0 / NULL
    assert maintenance.check_inverter_balance(sma, threshold_pp=8, min_day_kwh=20) is True
    assert len(notifikacije) == 1
    assert f'{losi} NE PROIZVODI' in notifikacije[0][1]
    # max jednom po datumu
    assert maintenance.check_inverter_balance(sma, threshold_pp=8, min_day_kwh=20) is False
    assert len(notifikacije) == 1


def test_inverter_podbacuje(sma, notifikacije):
    _inv_povijest(sma)
    _inv_jucer(sma, 10.0, 20.0)                 # INV1 33% umjesto 50%
    assert maintenance.check_inverter_balance(sma, threshold_pp=8, min_day_kwh=20) is True
    assert 'INV1 podbacuje' in notifikacije[0][1]


def test_inverter_danasnji_dan_se_ne_gleda(sma, notifikacije):
    _inv_povijest(sma, kraj=1)
    sma.execute('INSERT INTO sma_dnevna (datum, pv_kwh_inv1, pv_kwh_inv2) VALUES (?,?,?)',
                (date.today().isoformat(), 0.0, 25.0))
    sma.commit()
    assert maintenance.check_inverter_balance(sma, threshold_pp=8, min_day_kwh=20) is False
    assert notifikacije == []


def test_inverter_oblacan_dan_bez_alarma(sma, notifikacije):
    _inv_povijest(sma)
    _inv_jucer(sma, 0.0, 3.0)                   # ispod min_day_kwh => gleda se zadnji dobar dan
    assert maintenance.check_inverter_balance(sma, threshold_pp=8, min_day_kwh=20) is False
    assert notifikacije == []


def test_inverter_marker_tek_nakon_slanja(sma, bez_kanala):
    _inv_povijest(sma)
    _inv_jucer(sma, 0.0, 25.0)
    assert maintenance.check_inverter_balance(sma, threshold_pp=8, min_day_kwh=20) is False
    assert maintenance._get_cfg(sma, '_inv_balance_last_date') == ''
    maintenance.check_inverter_balance(sma, threshold_pp=8, min_day_kwh=20)
    assert len(bez_kanala) == 2
