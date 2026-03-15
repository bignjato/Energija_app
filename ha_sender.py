#!/usr/bin/env python3
"""
Home Assistant Energy integracija
Šalje HEP podatke na doma.infobot.cc HA instancu
"""

import sqlite3
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "pip", "install", "requests", "--break-system-packages", "-q"])
    import requests

# ─── Konfiguracija ─────────────────────────────────────────────────────────────
HA_URL      = os.getenv("HA_URL",   "https://doma.infobot.cc")
HA_TOKEN    = os.getenv("HA_TOKEN", "YOUR_LONG_LIVED_ACCESS_TOKEN")
DB_PATH     = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "hep_energy.db")))
LOG_PATH    = Path(__file__).parent / "ha_sender.log"

# Ime senzora u Home Assistantu (prilagodite po potrebi)
SENSOR_PREFIX = "sensor.hep_ods"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("ha_sender")


# ─── HA API ───────────────────────────────────────────────────────────────────

class HomeAssistantAPI:
    def __init__(self, url: str, token: str):
        self.url   = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

    def test_connection(self) -> bool:
        try:
            r = requests.get(f"{self.url}/api/", headers=self.headers, timeout=10, verify=False)
            if r.ok:
                log.info("✓ HA API dostupan: %s (verzija: %s)", self.url, r.json().get("version","?"))
                return True
            log.error("HA API greška: %s %s", r.status_code, r.text[:200])
            return False
        except Exception as e:
            log.error("HA API nedostupan: %s", e)
            return False

    def set_state(self, entity_id: str, state: float | str, attributes: dict = None) -> bool:
        """Postavi stanje senzora u HA."""
        payload = {
            "state": str(state),
            "attributes": attributes or {}
        }
        try:
            r = requests.post(
                f"{self.url}/api/states/{entity_id}",
                headers=self.headers,
                json=payload,
                timeout=10,
                verify=False
            )
            if r.ok:
                log.debug("✓ %s = %s", entity_id, state)
                return True
            log.warning("✗ %s: %s", entity_id, r.text[:100])
            return False
        except Exception as e:
            log.error("Greška slanja %s: %s", entity_id, e)
            return False

    def post_statistics(self, statistic_id: str, unit: str, rows: list) -> bool:
        """
        Šalje statistike u HA Energy dashboard putem recorder/import API.
        rows = [{"start": "ISO8601", "sum": float, "state": float}]
        """
        payload = {
            "id": 1,
            "type": "recorder/import_statistics",
            "metadata": {
                "has_mean": False,
                "has_sum": True,
                "name": statistic_id.replace("_", " ").title(),
                "source": "recorder",
                "statistic_id": statistic_id,
                "unit_of_measurement": unit,
            },
            "stats": [
                {
                    "start": row["start"],
                    "sum":   row["sum"],
                    "state": row.get("state", row["sum"]),
                }
                for row in rows
            ]
        }

        try:
            # WebSocket API za statistike (REST fallback)
            r = requests.post(
                f"{self.url}/api/services/recorder/import_statistics",
                headers=self.headers,
                json=payload,
                timeout=30,
                verify=False
            )
            return r.ok
        except Exception as e:
            log.error("Greška import_statistics: %s", e)
            return False


# ─── Čitanje iz baze ─────────────────────────────────────────────────────────

def get_zadnje_satno(conn: sqlite3.Connection, mjerno_mjesto: str | None = None) -> dict:
    """Dohvati zadnje nenulto satno ocitanje s kumulativnom vrijednosti."""
    where = "WHERE mjerno_mjesto = ? AND kwh_plus > 0" if mjerno_mjesto else "WHERE kwh_plus > 0"
    params = (mjerno_mjesto,) if mjerno_mjesto else ()

    row = conn.execute(f"""
        SELECT * FROM ocitanja_satna {where}
        ORDER BY ts DESC LIMIT 1
    """, params).fetchone()
    if not row:
        return {}

    result = dict(row)

    # Kumulativni zbroj za total_increasing senzor u HA
    where_cum = "WHERE mjerno_mjesto = ? AND ts <= ?" if mjerno_mjesto else "WHERE ts <= ?"
    params_cum = (mjerno_mjesto, result["ts"]) if mjerno_mjesto else (result["ts"],)
    cum = conn.execute(f"""
        SELECT SUM(kwh_plus), SUM(kwh_minus) FROM ocitanja_satna {where_cum}
    """, params_cum).fetchone()
    result["kwh_plus_kumulativ"]  = round(cum[0] or 0, 4)
    result["kwh_minus_kumulativ"] = round(cum[1] or 0, 4)
    return result


def get_satna_za_dan(conn: sqlite3.Connection, datum: str | None = None,
                     mjerno_mjesto: str | None = None) -> list:
    """Dohvati satna očitanja za zadani dan."""
    if not datum:
        datum = datetime.now().strftime("%Y-%m-%d")
    params = [f"{datum}%"]
    where_extra = ""
    if mjerno_mjesto:
        where_extra = "AND mjerno_mjesto = ?"
        params.append(mjerno_mjesto)

    rows = conn.execute(f"""
        SELECT * FROM ocitanja_satna
        WHERE ts LIKE ? {where_extra}
        ORDER BY ts ASC
    """, params).fetchall()
    return [dict(r) for r in rows]


def get_dnevna_ukupno(conn: sqlite3.Connection, mjerno_mjesto: str | None = None,
                      dani: int = 30) -> list:
    """Dohvati dnevne vrijednosti za zadani period."""
    od = (datetime.now() - timedelta(days=dani)).strftime("%Y-%m-%d")
    where_extra = ""
    params = [od]
    if mjerno_mjesto:
        where_extra = "AND mjerno_mjesto = ?"
        params.append(mjerno_mjesto)

    rows = conn.execute(f"""
        SELECT * FROM ocitanja_dnevna
        WHERE datum >= ? {where_extra}
        ORDER BY datum ASC
    """, params).fetchall()
    return [dict(r) for r in rows]


def get_mjerna_mjesta(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT * FROM mjerna_mjesta").fetchall()
    return [dict(r) for r in rows]


# ─── Slanje u HA ──────────────────────────────────────────────────────────────

def posalji_trenutno_stanje(ha: HomeAssistantAPI, conn: sqlite3.Connection):
    """Šalje trenutno stanje senzora u HA (za prikaz na dashboardu)."""
    mjesta = get_mjerna_mjesta(conn)
    if not mjesta:
        log.warning("Nema mjernih mjesta u bazi!")
        return

    for m in mjesta:
        mid   = m["id"]
        naziv = m.get("naziv", mid)
        safe  = mid.lower().replace("-", "_").replace(" ", "_")

        ocitanje = get_zadnje_satno(conn, mid)
        if not ocitanje:
            continue

        kwh_plus  = ocitanje.get("kwh_plus_kumulativ") or ocitanje.get("kwh_plus", 0) or 0
        kwh_minus = ocitanje.get("kwh_minus_kumulativ") or ocitanje.get("kwh_minus", 0) or 0

        # Senzor - ukupna potrošnja
        ha.set_state(
            entity_id=f"{SENSOR_PREFIX}_{safe}_potrosnja",
            state=round(kwh_plus, 4),
            attributes={
                "unit_of_measurement":  "kWh",
                "device_class":         "energy",
                "state_class":          "total_increasing",
                "friendly_name":        f"HEP {naziv} - Potrošnja",
                "icon":                 "mdi:lightning-bolt",
                "attribution":          "HEP ODS mjerenje.hep.hr",
                "mjerno_mjesto":        mid,
                "adresa":               m.get("adresa", ""),
                "zadnje_ocitanje":      ocitanje.get("ts", ""),
            }
        )

        # Senzor - predaja u mrežu (ako postoji)
        if kwh_minus and kwh_minus > 0:
            ha.set_state(
                entity_id=f"{SENSOR_PREFIX}_{safe}_predaja",
                state=round(kwh_minus, 4),
                attributes={
                    "unit_of_measurement":  "kWh",
                    "device_class":         "energy",
                    "state_class":          "total_increasing",
                    "friendly_name":        f"HEP {naziv} - Predaja u mrežu",
                    "icon":                 "mdi:solar-power",
                }
            )

        log.info("✓ Senzori ažurirani za: %s", naziv)


def posalji_statistike(ha: HomeAssistantAPI, conn: sqlite3.Connection, dani: int = 7):
    """Šalje historijske statistike za Energy dashboard."""
    mjesta = get_mjerna_mjesta(conn)

    for m in mjesta:
        mid  = m["id"]
        safe = mid.lower().replace("-", "_").replace(" ", "_")

        dnevni = get_dnevna_ukupno(conn, mid, dani)
        if not dnevni:
            continue

        # Kumulativne vrijednosti (total_increasing)
        cumsum = 0
        stat_rows = []
        for d in dnevni:
            cumsum += (d.get("kwh_plus") or 0)
            stat_rows.append({
                "start": f"{d['datum']}T00:00:00+01:00",
                "sum":   round(cumsum, 4),
                "state": round(d.get("kwh_plus") or 0, 4),
            })

        statistic_id = f"{SENSOR_PREFIX}_{safe}_potrosnja"
        ok = ha.post_statistics(statistic_id, "kWh", stat_rows)
        log.info("%s statistike %s: %d zapisa",
                 "✓" if ok else "✗", statistic_id, len(stat_rows))


def posalji_energy_config(ha: HomeAssistantAPI, conn: sqlite3.Connection):
    """
    Prikaži upute za Energy dashboard konfiguraciju.
    HA Energy dashboard se konfigurira ručno kroz UI.
    """
    mjesta = get_mjerna_mjesta(conn)
    print("\n" + "="*60)
    print("🏠 HOME ASSISTANT - ENERGY DASHBOARD KONFIGURACIJA")
    print("="*60)
    print("\nIdite na: Settings → Dashboards → Energy\n")
    print("Dodajte sljedeće senzore:\n")

    for m in mjesta:
        mid  = m["id"]
        safe = mid.lower().replace("-", "_").replace(" ", "_")
        print(f"📍 {m.get('naziv', mid)}:")
        print(f"   Grid consumption: {SENSOR_PREFIX}_{safe}_potrosnja")
        predaja = f"{SENSOR_PREFIX}_{safe}_predaja"
        print(f"   Return to grid:   {predaja}  (samo ako imate solarnu)")
        print()

    print("="*60)


# ─── Glavni tok ──────────────────────────────────────────────────────────────

def main():
    import argparse
    import urllib3
    urllib3.disable_warnings()  # Ignoriraj SSL upozorenja za lokalni HA

    parser = argparse.ArgumentParser(description="HEP → Home Assistant sender")
    parser.add_argument("--url",   default=None, help="HA URL (default: env HA_URL)")
    parser.add_argument("--token", default=None, help="HA token (default: env HA_TOKEN)")
    parser.add_argument("--dani",  type=int, default=7, help="Koliko dana statistike poslati")
    parser.add_argument("--samo-stanje", action="store_true", help="Samo trenutno stanje, bez statistika")
    args = parser.parse_args()

    url   = args.url   or HA_URL
    token = args.token or HA_TOKEN

    if token == "YOUR_LONG_LIVED_ACCESS_TOKEN":
        print("⚠️  Postavite HA_TOKEN environment varijablu ili --token argument!")
        print("   HA token generirajte na: Profil → Long-lived access tokens")
        return False

    ha   = HomeAssistantAPI(url, token)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if not ha.test_connection():
        conn.close()
        return False

    posalji_trenutno_stanje(ha, conn)

    if not args.samo_stanje:
        posalji_statistike(ha, conn, args.dani)

    posalji_energy_config(ha, conn)

    conn.close()
    log.info("✅ HA sinkronizacija završena!")
    return True


if __name__ == "__main__":
    import sys
    ok = main()
    sys.exit(0 if ok else 1)
