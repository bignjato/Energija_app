#!/usr/bin/env python3
"""
HEP ODS Mjerni podaci - Scraper i SQLite pohrana
API: https://mjerenje.hep.hr/mjerenja/v1/
Endpoint: POST api/data/omm/{sifra}/krivulja/mjesec/{MM.YYYY}/smjer/{smjer}
"""

import requests
import sqlite3
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

BASE_URL = "https://mjerenje.hep.hr/mjerenja/v1"
DB_PATH  = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "hep_energy.db")))
LOG_PATH = Path(__file__).parent / "hep_scraper.log"
USERNAME = os.getenv("HEP_USERNAME", "vas_korisnik@email.com")
PASSWORD = os.getenv("HEP_PASSWORD", "vasa_lozinka")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()]
)
log = logging.getLogger("hep_scraper")


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mjerna_mjesta (
            id TEXT PRIMARY KEY, naziv TEXT, adresa TEXT,
            oib TEXT, tip TEXT, napon TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS ocitanja_15min (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mjerno_mjesto TEXT NOT NULL, ts TEXT NOT NULL,
            kwh_plus REAL, kwh_minus REAL, kvarh_plus REAL, kvarh_minus REAL,
            UNIQUE(mjerno_mjesto, ts)
        );
        CREATE TABLE IF NOT EXISTS ocitanja_satna (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mjerno_mjesto TEXT NOT NULL, ts TEXT NOT NULL,
            kwh_plus REAL, kwh_minus REAL, kvarh_plus REAL, kvarh_minus REAL,
            UNIQUE(mjerno_mjesto, ts)
        );
        CREATE TABLE IF NOT EXISTS ocitanja_dnevna (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mjerno_mjesto TEXT NOT NULL, datum TEXT NOT NULL,
            kwh_plus REAL, kwh_minus REAL, kvarh_plus REAL, kvarh_minus REAL,
            UNIQUE(mjerno_mjesto, datum)
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT (datetime('now')),
            tip TEXT, status TEXT, poruka TEXT, zapisi INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_15min_ts ON ocitanja_15min(mjerno_mjesto, ts);
        CREATE INDEX IF NOT EXISTS idx_satna_ts ON ocitanja_satna(mjerno_mjesto, ts);
        CREATE INDEX IF NOT EXISTS idx_dnevna_ts ON ocitanja_dnevna(mjerno_mjesto, datum);
    """)
    conn.commit()
    log.info("Baza inicijalizirana: %s", DB_PATH)


class HEPSession:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://mjerenje.hep.hr",
            "Referer": "https://mjerenje.hep.hr/mjerenja/",
        })
        self.token = None
        self.logged_in = False
        self.kupci = []

    def login(self, username, password):
        log.info("Prijava: %s", username)
        try:
            r = self.session.post(
                f"{BASE_URL}/api/user/login",
                json={"Username": username, "Password": password},
                timeout=15
            )
            if r.status_code == 200:
                data = r.json()
                self.token = data.get("Token")
                self.kupci = data.get("KupacList", [])
                self.session.headers["Authorization"] = f"Bearer {self.token}"
                self.logged_in = True
                log.info("Prijava uspjesna! Kupaca: %d", len(self.kupci))
                return True
            log.error("Prijava neuspjesna: %s %s", r.status_code, r.text[:200])
            return False
        except Exception as e:
            log.error("Greska pri prijavi: %s", e)
            return False

    def get_mjerna_mjesta(self):
        mjesta = []
        for kupac in self.kupci:
            for omm in kupac.get("OmmList", []):
                mjesta.append({
                    "id":     omm.get("Sifra", ""),
                    "naziv":  kupac.get("Naziv", ""),
                    "adresa": omm.get("Adresa", "").strip(),
                    "oib":    kupac.get("Oib", ""),
                    "tip":    "Potrosac" if omm.get("Potrosac") else "Proizvodjac",
                    "napon":  "",
                    "mjesec_od": omm.get("MjesecOd", "")[:7],
                    "mjesec_do": omm.get("MjesecDo", "")[:7],
                })
        log.info("Pronadjeno %d mjernih mjesta", len(mjesta))
        return mjesta

    def get_krivulje_mjesec(self, omm_sifra, mjesec_fmt, smjer="A-"):
        """
        Dohvati 15-min krivulje za jedan mjesec.
        mjesec_fmt: 'MM.YYYY' (npr. '03.2026')
        smjer: 'A-' vraca A+ potrosnju, 'A%2B' za predaju
        """
        url = f"{BASE_URL}/api/data/omm/{omm_sifra}/krivulja/mjesec/{mjesec_fmt}/smjer/{smjer}"
        try:
            r = self.session.post(url, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    log.info("  %s smjer=%s: %d zapisa", mjesec_fmt, smjer, len(data))
                    return data
            log.debug("  %s smjer=%s: %s", mjesec_fmt, smjer, r.text[:100])
            return []
        except Exception as e:
            log.error("Greska get_krivulje: %s", e)
            return []


def spremi_mjerna_mjesta(conn, mjesta):
    for m in mjesta:
        conn.execute(
            "INSERT OR REPLACE INTO mjerna_mjesta (id,naziv,adresa,oib,tip,napon) VALUES (?,?,?,?,?,?)",
            (m["id"], m["naziv"], m["adresa"], m.get("oib",""), m["tip"], m.get("napon",""))
        )
    conn.commit()


def parse_vrijednost(v):
    """Parsira '1,64800000' u float 1.648"""
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "."))
    except:
        return 0.0


def spremi_krivulje(conn, podaci, mjerno_mjesto, je_minus=False):
    """
    Spremi 15-min krivulje.
    Format: {"Status":"0","Sifra":"...","Obis":"LP: A+_T0","Datum":"2026-02-28T23:45:00","Value":"1,648"}
    """
    if not podaci:
        return 0
    saved = 0
    for row in podaci:
        ts  = row.get("Datum") or row.get("datum") or row.get("DateTime")
        val = parse_vrijednost(row.get("Value") or row.get("Vrijednost") or 0)
        # HEP API vraca kWh za 15-min period, ali sumiran kao da je satni
        # Treba dijeliti s 4 da dobijemo pravi 15-min kWh
        val = val / 4.0
        if not ts:
            continue
        try:
            if je_minus:
                conn.execute(
                    "INSERT OR IGNORE INTO ocitanja_15min (mjerno_mjesto,ts,kwh_plus,kwh_minus) VALUES (?,?,0,?)",
                    (mjerno_mjesto, ts, val)
                )
            else:
                conn.execute(
                    """INSERT INTO ocitanja_15min (mjerno_mjesto,ts,kwh_plus,kwh_minus)
                       VALUES (?,?,?,0)
                       ON CONFLICT(mjerno_mjesto,ts) DO UPDATE SET kwh_plus=excluded.kwh_plus""",
                    (mjerno_mjesto, ts, val)
                )
            saved += 1
        except sqlite3.Error as e:
            log.debug("Skip row: %s", e)
    conn.commit()
    return saved


def agregacija(conn, mjerno_mjesto):
    conn.execute("""
        INSERT OR REPLACE INTO ocitanja_satna
            (mjerno_mjesto, ts, kwh_plus, kwh_minus, kvarh_plus, kvarh_minus)
        SELECT mjerno_mjesto,
               strftime('%Y-%m-%dT%H:00:00', ts),
               SUM(kwh_plus), SUM(kwh_minus),
               SUM(kvarh_plus), SUM(kvarh_minus)
        FROM ocitanja_15min
        WHERE mjerno_mjesto=?
        GROUP BY mjerno_mjesto, strftime('%Y-%m-%dT%H:00:00', ts)
    """, (mjerno_mjesto,))
    conn.execute("""
        INSERT OR REPLACE INTO ocitanja_dnevna
            (mjerno_mjesto, datum, kwh_plus, kwh_minus, kvarh_plus, kvarh_minus)
        SELECT mjerno_mjesto,
               date(ts),
               SUM(kwh_plus), SUM(kwh_minus),
               SUM(kvarh_plus), SUM(kvarh_minus)
        FROM ocitanja_15min
        WHERE mjerno_mjesto=?
        GROUP BY mjerno_mjesto, date(ts)
    """, (mjerno_mjesto,))
    conn.commit()
    n_sat = conn.execute("SELECT COUNT(*) FROM ocitanja_satna WHERE mjerno_mjesto=?", (mjerno_mjesto,)).fetchone()[0]
    n_dan = conn.execute("SELECT COUNT(*) FROM ocitanja_dnevna WHERE mjerno_mjesto=?", (mjerno_mjesto,)).fetchone()[0]
    return n_sat, n_dan


def get_mjeseci_za_dohvat(dani_unazad):
    """Vrati listu mjeseci u formatu MM.YYYY za zadani period."""
    now = datetime.now()
    start = now - timedelta(days=dani_unazad)
    mieseci = []
    d = start.replace(day=1)
    while d <= now:
        mieseci.append(d.strftime("%m.%Y"))
        # Sljedeci mjesec
        if d.month == 12:
            d = d.replace(year=d.year+1, month=1)
        else:
            d = d.replace(month=d.month+1)
    return list(dict.fromkeys(mieseci))  # deduplikacija


def sync(username=None, password=None, dani_unazad=7):
    username = username or USERNAME
    password = password or PASSWORD

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    hep = HEPSession()
    if not hep.login(username, password):
        conn.execute("INSERT INTO sync_log (tip,status,poruka) VALUES (?,?,?)",
                     ("login", "ERROR", "Prijava neuspjesna"))
        conn.commit()
        conn.close()
        return False

    mjesta = hep.get_mjerna_mjesta()
    if not mjesta:
        log.warning("Nema mjernih mjesta!")
        conn.close()
        return False

    spremi_mjerna_mjesta(conn, mjesta)

    mieseci = get_mjeseci_za_dohvat(dani_unazad)
    log.info("Dohvacam mjesece: %s", mieseci)

    total_saved = 0
    for m in mjesta:
        mid = m["id"]
        log.info("Mjerno mjesto: %s (%s)", m["naziv"], mid)

        for mj in mieseci:
            # smjer=P vraca A+ (potrosnja iz mreze)
            krivulje = hep.get_krivulje_mjesec(mid, mj, smjer="P")
            n = spremi_krivulje(conn, krivulje, mid, je_minus=False)
            log.info("  %s potrosnja (A+): %d zapisa", mj, n)
            total_saved += n

            # smjer=R vraca A- (predaja u mrezu - solarna)
            krivulje_r = hep.get_krivulje_mjesec(mid, mj, smjer="R")
            if krivulje_r:
                nm = spremi_krivulje(conn, krivulje_r, mid, je_minus=True)
                log.info("  %s predaja (A-): %d zapisa", mj, nm)
                total_saved += nm

        n_sat, n_dan = agregacija(conn, mid)
        log.info("  Ukupno satna: %d, dnevna: %d", n_sat, n_dan)

    conn.execute("INSERT INTO sync_log (tip,status,zapisi,poruka) VALUES (?,?,?,?)",
                 ("sync", "OK", total_saved, f"Sinkronizirano {len(mjesta)} mjernih mjesta, {len(mieseci)} mjeseci"))
    conn.commit()
    conn.close()
    log.info("Gotovo! %d novih 15-min zapisa.", total_saved)
    return True


if __name__ == "__main__":
    import argparse, sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", "-u", default=None)
    parser.add_argument("--password", "-p", default=None)
    parser.add_argument("--dani", "-d", type=int, default=7)
    args = parser.parse_args()
    ok = sync(args.username, args.password, args.dani)
    sys.exit(0 if ok else 1)
