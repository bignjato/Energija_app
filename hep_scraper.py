#!/usr/bin/env python3
"""
HEP ODS Mjerni podaci - Scraper i SQLite pohrana
API: https://mjerenje.hep.hr/mjerenja/v1.2/ (verzija se mijenja - vidi detect_base_url)
Endpoint: POST api/data/omm/{sifra}/krivulja/mjesec/{MM.YYYY}/smjer/{smjer}
"""

import requests
import sqlite3
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

BASE_URL = "https://mjerenje.hep.hr/mjerenja/v1.2"
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
        CREATE TABLE IF NOT EXISTS hep_registri (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mjerno_mjesto TEXT NOT NULL, datum TEXT NOT NULL,
            obis TEXT NOT NULL, value REAL,
            UNIQUE(mjerno_mjesto, datum, obis)
        );
        CREATE INDEX IF NOT EXISTS idx_15min_ts ON ocitanja_15min(mjerno_mjesto, ts);
        CREATE INDEX IF NOT EXISTS idx_satna_ts ON ocitanja_satna(mjerno_mjesto, ts);
        CREATE INDEX IF NOT EXISTS idx_dnevna_ts ON ocitanja_dnevna(mjerno_mjesto, datum);
    """)
    conn.commit()
    log.info("Baza inicijalizirana: %s", DB_PATH)


def detect_base_url():
    """Otkrij trenutni API prefiks iz HEP SPA bundle-a.

    HEP povremeno mijenja verziju puta (v1 -> v1.1 -> ...), sto obara login
    (404/405). Ova funkcija procita apiUrl iz JS bundle-a i vrati npr.
    'https://mjerenje.hep.hr/mjerenja/v1.1'. Vraca None ako ne uspije.
    """
    import re
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        idx = s.get("https://mjerenje.hep.hr/mjerenja/", timeout=15).text
        for f in set(re.findall(r'(main[.-][A-Za-z0-9_]+\.js)', idx)):
            t = s.get("https://mjerenje.hep.hr/mjerenja/" + f, timeout=20).text
            for u in re.findall(r'apiUrl\s*[=:]\s*["\']([^"\']+)["\']', t):
                m = re.match(r'(v[0-9.]+)/api/?', u)
                if m:
                    return "https://mjerenje.hep.hr/mjerenja/" + m.group(1)
    except Exception as e:
        log.warning("detect_base_url: %s", e)
    return None


class HEPSession:
    def __init__(self):
        from netutil import retry_session
        # HEP API koristi POST za read endpointe — POST retry je siguran
        self.session = retry_session(allow_post=True)
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
        # Neuspjeli dohvati krivulja (HTTP greska / iznimka) — sync() ih
        # pretvara u ERROR red u sync_log. Prazan mjesec NIJE greska.
        self.greske = []
        self.greske_registri = []
        self._detect_pokusan = False

    def login(self, username, password):
        log.info("Prijava: %s", username)
        global BASE_URL
        for attempt in (1, 2):
            try:
                r = self.session.post(
                    f"{BASE_URL}/api/user/login",
                    json={"Username": username, "Password": password},
                    timeout=15
                )
            except Exception as e:
                log.error("Greska pri prijavi: %s", e)
                return False
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    # 200 s ne-JSON tijelom (HTML odrzavanje, captive portal...).
                    # Tijelo se ne logira — moglo bi sadrzavati token.
                    log.error("Prijava neuspjesna: odgovor 200 nije JSON (%s, %d B)",
                              r.headers.get("Content-Type", "?"), len(r.content or b""))
                    return False
                if not isinstance(data, (list, dict)):
                    log.error("Prijava neuspjesna: neocekivan JSON tip %s", type(data).__name__)
                    return False
                # Novi API: response je list kupac-objekata; auth preko
                # Set-Cookie 'accessToken'. Stari: {"Token":..,"KupacList":..}.
                if isinstance(data, list):
                    self.kupci = data
                    self.token = self.session.cookies.get("accessToken")
                else:
                    self.token = data.get("Token")
                    self.kupci = data.get("KupacList", [])
                    if self.token:
                        self.session.headers["Authorization"] = f"Bearer {self.token}"
                self.logged_in = True
                log.info("Prijava uspjesna! Kupaca: %d", len(self.kupci))
                return True
            # 404/405 = promijenjen API path/verzija -> auto-detekcija + retry
            if r.status_code in (404, 405) and attempt == 1:
                det = detect_base_url()
                if det and det != BASE_URL:
                    log.warning("HEP API verzija promijenjena: %s -> %s", BASE_URL, det)
                    BASE_URL = det
                    continue
            log.error("Prijava neuspjesna: %s %s", r.status_code, r.text[:200])
            return False
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

    def _post_data(self, path):
        """POST na data endpoint (path pocinje s '/api/...').

        404/405 = HEP je opet promijenio API prefiks: jednom po sesiji probaj
        detect_base_url() i, ako vrati drugu bazu, ponovi zahtjev na njoj.
        """
        global BASE_URL
        r = self.session.post(f"{BASE_URL}{path}", timeout=30)
        if r.status_code in (404, 405) and not self._detect_pokusan:
            self._detect_pokusan = True
            det = detect_base_url()
            if det and det != BASE_URL:
                log.warning("HEP API verzija promijenjena: %s -> %s", BASE_URL, det)
                BASE_URL = det
                r = self.session.post(f"{BASE_URL}{path}", timeout=30)
        return r

    def get_krivulje_mjesec(self, omm_sifra, mjesec_fmt, smjer="A-"):
        """
        Dohvati 15-min krivulje za jedan mjesec.
        mjesec_fmt: 'MM.YYYY' (npr. '03.2026')
        smjer: 'A-' vraca A+ potrosnju, 'A%2B' za predaju

        Vraca [] i kad mjesec jos nema podataka i kad dohvat ne uspije;
        neuspjeh (HTTP greska / iznimka) se dodatno biljezi u self.greske.
        """
        path = f"/api/data/omm/{omm_sifra}/krivulja/mjesec/{mjesec_fmt}/smjer/{smjer}"
        opis = f"krivulja {mjesec_fmt} smjer={smjer}"
        try:
            r = self._post_data(path)
            if r.status_code == 200:
                data = r.json() if r.content else []
                if isinstance(data, list) and data:
                    log.info("  %s smjer=%s: %d zapisa", mjesec_fmt, smjer, len(data))
                    return data
                if data:
                    # 200, ali nije lista zapisa — nije HTTP greska, no vrijedi vidjeti
                    log.warning("  %s smjer=%s: neocekivan odgovor: %s",
                                mjesec_fmt, smjer, r.text[:100])
                else:
                    log.debug("  %s smjer=%s: nema podataka", mjesec_fmt, smjer)
                return []
            if r.status_code == 204:
                log.debug("  %s smjer=%s: 204 (nema podataka)", mjesec_fmt, smjer)
                return []
            log.warning("  %s smjer=%s: HTTP %s %s", mjesec_fmt, smjer,
                        r.status_code, r.text[:100])
            self.greske.append(f"{opis}: HTTP {r.status_code}")
            return []
        except Exception as e:
            log.warning("Greska get_krivulje %s smjer=%s: %s", mjesec_fmt, smjer, e)
            self.greske.append(f"{opis}: {type(e).__name__}")
            return []

    def get_registri_mjesec(self, omm_sifra, mjesec_fmt):
        """Sluzbena stanja brojila po tarifnim registrima na kraju mjeseca.

        Format: [{"Sifra":"...","Obis":"REG: A+_T1","Datum":"2026-05-31T23:59:00","Value":"36239,9450"}]
        204 No Content = mjesec jos nije zakljucen (tekuci).
        """
        path = f"/api/data/omm/{omm_sifra}/mjesec/{mjesec_fmt}/registri"
        try:
            r = self._post_data(path)
            if r.status_code == 200:
                data = r.json() if r.content else []
                if isinstance(data, list) and data:
                    log.info("  %s registri: %d zapisa", mjesec_fmt, len(data))
                    return data
                return []
            if r.status_code == 204:
                return []
            log.warning("  %s registri: HTTP %s %s", mjesec_fmt, r.status_code, r.text[:100])
            self.greske_registri.append(f"registri {mjesec_fmt}: HTTP {r.status_code}")
            return []
        except Exception as e:
            log.warning("Greska get_registri %s: %s", mjesec_fmt, e)
            self.greske_registri.append(f"registri {mjesec_fmt}: {type(e).__name__}")
            return []


def spremi_mjerna_mjesta(conn, mjesta):
    for m in mjesta:
        conn.execute(
            "INSERT OR REPLACE INTO mjerna_mjesta (id,naziv,adresa,oib,tip,napon) VALUES (?,?,?,?,?,?)",
            (m["id"], m["naziv"], m["adresa"], m.get("oib",""), m["tip"], m.get("napon",""))
        )
    conn.commit()


def spremi_registri(conn, podaci, mjerno_mjesto):
    saved = 0
    for row in podaci:
        ts   = row.get("Datum") or ""
        obis = (row.get("Obis") or "").replace("REG: ", "").strip()
        val  = parse_vrijednost(row.get("Value"))
        if not ts or not obis:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO hep_registri (mjerno_mjesto, datum, obis, value) VALUES (?,?,?,?)",
            (mjerno_mjesto, ts[:10], obis, val)
        )
        saved += 1
    conn.commit()
    return saved


def parse_vrijednost(v):
    """Parsira '1,64800000' u float 1.648"""
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError) as e:
        log.warning("parse_vrijednost: neuspjelo parsiranje %r: %s", v, e)
        return 0.0


def _sada():
    """Trenutno lokalno vrijeme (kontejneri rade s TZ=Europe/Zagreb)."""
    return datetime.now()


def spremi_krivulje(conn, podaci, mjerno_mjesto, je_minus=False):
    """
    Spremi 15-min krivulje.
    Format: {"Status":"0","Sifra":"...","Obis":"LP: A+_T0","Datum":"2026-02-28T23:45:00","Value":"1,648"}
    Datum je KRAJ intervala: uzorak T pokriva (T-15min, T].
    """
    if not podaci:
        return 0
    saved = 0
    # HEP vraca cijeli mjesec unaprijed s nulama — uzorke ciji interval jos
    # nije zavrsio ne spremamo. Usporedba po vremenu, ne po datumu: uzorak
    # 'sutra T00:00:00' je zadnji interval danasnjeg dana.
    sada = _sada().strftime("%Y-%m-%dT%H:%M:%S")
    for row in podaci:
        ts  = row.get("Datum") or row.get("datum") or row.get("DateTime")
        if ts and ts[:19] > sada:
            continue
        val = parse_vrijednost(row.get("Value") or row.get("Vrijednost") or 0)
        # HEP API vraca kWh za 15-min period, ali sumiran kao da je satni
        # Treba dijeliti s 4 da dobijemo pravi 15-min kWh
        val = val / 4.0
        if not ts:
            continue
        try:
            if je_minus:
                # Predaja A-: upsert ažurira SAMO kwh_minus, ne dira kwh_plus
                conn.execute(
                    """INSERT INTO ocitanja_15min (mjerno_mjesto,ts,kwh_plus,kwh_minus)
                       VALUES (?,?,0,?)
                       ON CONFLICT(mjerno_mjesto,ts) DO UPDATE SET kwh_minus=excluded.kwh_minus""",
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
    """Ponovno izgradi ocitanja_satna i ocitanja_dnevna iz ocitanja_15min.

    HEP uzorak nosi oznaku KRAJA intervala (T pokriva (T-15min, T]), pa se
    svaki uzorak pripisuje satu/danu u kojem interval POCINJE (ts - 15 min):
    'D+1T00:00:00' je zadnji interval dana D. Kljucevi se time pomicu u odnosu
    na staro grupiranje po sirovom ts, pa INSERT OR REPLACE ne bi uklonio
    zastarjele redove — zato brisanje + ponovni upis u JEDNOJ transakciji
    (citaci ne vide polovicnu tablicu, pad = rollback).
    """
    m = {'m': mjerno_mjesto}
    conn.commit()  # zakljuci eventualnu otvorenu transakciju pozivatelja
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Trailing nula-uzorci = HEP jos nije isporucio podatke (15-min uzorak
        # s 0 kWh u oba smjera nakon zadnjeg stvarnog ne postoji). Brisu se
        # PRIJE agregacije, pa sati/dani bez stvarnih podataka ne nastaju i
        # ne kvare n_dana u procjenama i grafove.
        conn.execute("""
            DELETE FROM ocitanja_15min WHERE mjerno_mjesto=:m
              AND kwh_plus=0 AND kwh_minus=0
              AND ts > (SELECT COALESCE(MAX(ts),'1900-01-01') FROM ocitanja_15min
                        WHERE mjerno_mjesto=:m AND (kwh_plus>0 OR kwh_minus>0))
        """, m)
        conn.execute("DELETE FROM ocitanja_satna WHERE mjerno_mjesto=:m", m)
        conn.execute("DELETE FROM ocitanja_dnevna WHERE mjerno_mjesto=:m", m)
        conn.execute("""
            INSERT INTO ocitanja_satna
                (mjerno_mjesto, ts, kwh_plus, kwh_minus, kvarh_plus, kvarh_minus)
            SELECT mjerno_mjesto,
                   strftime('%Y-%m-%dT%H:00:00', ts, '-15 minutes') AS sat,
                   SUM(kwh_plus), SUM(kwh_minus),
                   SUM(kvarh_plus), SUM(kvarh_minus)
            FROM ocitanja_15min
            WHERE mjerno_mjesto=:m AND datetime(ts) IS NOT NULL
            GROUP BY mjerno_mjesto, sat
        """, m)
        conn.execute("""
            INSERT INTO ocitanja_dnevna
                (mjerno_mjesto, datum, kwh_plus, kwh_minus, kvarh_plus, kvarh_minus)
            SELECT mjerno_mjesto,
                   date(ts, '-15 minutes') AS dan,
                   SUM(kwh_plus), SUM(kwh_minus),
                   SUM(kvarh_plus), SUM(kvarh_minus)
            FROM ocitanja_15min
            WHERE mjerno_mjesto=:m AND datetime(ts) IS NOT NULL
            GROUP BY mjerno_mjesto, dan
        """, m)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    n_sat = conn.execute("SELECT COUNT(*) FROM ocitanja_satna WHERE mjerno_mjesto=?", (mjerno_mjesto,)).fetchone()[0]
    n_dan = conn.execute("SELECT COUNT(*) FROM ocitanja_dnevna WHERE mjerno_mjesto=?", (mjerno_mjesto,)).fetchone()[0]
    return n_sat, n_dan


def agregiraj_sve(conn, preskoci=()):
    """Ponovno izgradi satnu/dnevnu tablicu za SVA mjerna mjesta iz baze.

    Neovisno o HEP portalu: agregati moraju pratiti ocitanja_15min i kad
    prijava ne uspije ili mjerno mjesto vise nije u odgovoru prijave (inace bi
    stari redovi, grupirani po sirovom ts, ostali do prvog uspjesnog synca).
    Greska na jednom mjernom mjestu ne smije prekinuti ostala ni sakriti
    izvorni razlog neuspjeha synca.
    """
    mjesta = [r[0] for r in conn.execute(
        "SELECT DISTINCT mjerno_mjesto FROM ocitanja_15min ORDER BY 1")]
    n = 0
    for mid in mjesta:
        if mid in preskoci:
            continue
        try:
            n_sat, n_dan = agregacija(conn, mid)
            log.info("Agregacija %s: satna %d, dnevna %d", mid, n_sat, n_dan)
            n += 1
        except sqlite3.Error as e:
            log.warning("Agregacija %s neuspjela: %s", mid, e)
    return n


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
        agregiraj_sve(conn)  # agregati ne ovise o dostupnosti portala
        conn.close()
        return False

    mjesta = hep.get_mjerna_mjesta()
    if not mjesta:
        log.warning("Nema mjernih mjesta!")
        conn.execute("INSERT INTO sync_log (tip,status,poruka) VALUES (?,?,?)",
                     ("sync", "ERROR", "Nema mjernih mjesta u odgovoru prijave"))
        conn.commit()
        agregiraj_sve(conn)
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

        # Registri: dohvati i 1 mjesec prije prvog (delta racun treba prethodno stanje)
        reg_mjeseci = list(mieseci)
        try:
            mm, yy = mieseci[0].split(".")
            mm, yy = int(mm), int(yy)
            mm -= 1
            if mm == 0:
                mm, yy = 12, yy - 1
            reg_mjeseci.insert(0, f"{mm:02d}.{yy}")
        except Exception:
            pass
        for mj in reg_mjeseci:
            registri = hep.get_registri_mjesec(mid, mj)
            if registri:
                nr = spremi_registri(conn, registri, mid)
                log.info("  %s registri: %d spremljeno", mj, nr)

        n_sat, n_dan = agregacija(conn, mid)
        log.info("  Ukupno satna: %d, dnevna: %d", n_sat, n_dan)

    # Mjerna mjesta koja postoje u bazi, ali ih prijava vise ne vraca
    agregiraj_sve(conn, preskoci={m["id"] for m in mjesta})

    poruka = f"Sinkronizirano {len(mjesta)} mjernih mjesta, {len(mieseci)} mjeseci"
    if hep.greske_registri:
        # Registri nisu kriticni za krivulje — ostaje OK, ali neka se vidi
        poruka += f"; registri: {len(hep.greske_registri)} neuspjelih dohvata"
    if hep.greske:
        # Ono sto je stiglo je spremljeno i agregirano, ali sync NIJE uspio
        poruka = (f"{len(hep.greske)} neuspjelih dohvata krivulja: "
                  + "; ".join(hep.greske[:5]))[:500]
        conn.execute("INSERT INTO sync_log (tip,status,zapisi,poruka) VALUES (?,?,?,?)",
                     ("sync", "ERROR", total_saved, poruka))
        conn.commit()
        conn.close()
        log.error("Sync neuspjesan: %s", poruka)
        return False

    conn.execute("INSERT INTO sync_log (tip,status,zapisi,poruka) VALUES (?,?,?,?)",
                 ("sync", "OK", total_saved, poruka))
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
