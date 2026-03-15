#!/usr/bin/env python3
"""
Generira dashboard.html iz SQLite baze podataka.
Pokrenite nakon hep_scraper.py da osvježite vizualizaciju.
"""

import sqlite3
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH        = Path(__file__).parent / "hep_energy.db"
TEMPLATE_PATH  = Path(__file__).parent / "dashboard.html"
OUTPUT_PATH    = Path(__file__).parent / "dashboard.html"


def export_data(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row

    satna = [dict(r) for r in conn.execute("""
        SELECT ts, kwh_plus, kwh_minus, kvarh_plus FROM ocitanja_satna
        WHERE ts >= datetime('now', '-7 days')
        ORDER BY ts ASC
    """).fetchall()]

    dnevna = [dict(r) for r in conn.execute("""
        SELECT datum, kwh_plus, kwh_minus FROM ocitanja_dnevna
        ORDER BY datum ASC
    """).fetchall()]

    min15 = [dict(r) for r in conn.execute("""
        SELECT ts, kwh_plus FROM ocitanja_15min
        WHERE ts >= datetime('now', '-3 days')
        ORDER BY ts ASC
    """).fetchall()]

    mm_row = conn.execute("SELECT * FROM mjerna_mjesta LIMIT 1").fetchone()
    mm = dict(mm_row) if mm_row else {}

    # Prosječni profil po satu
    all_sat = conn.execute("SELECT ts, kwh_plus FROM ocitanja_satna").fetchall()
    profil: dict[int, list] = {}
    for r in all_sat:
        try:
            h = int(r["ts"][11:13])
            profil.setdefault(h, []).append(r["kwh_plus"])
        except:
            pass
    profil_avg = {h: round(sum(v)/len(v), 4) for h, v in profil.items()}

    return {
        "mm":     mm,
        "satna":  satna,
        "dnevna": dnevna,
        "min15":  min15,
        "profil": profil_avg,
    }


def generate_dashboard(data: dict):
    """Zamijeni DATA objekt u HTML datoteci novim podacima."""
    html = TEMPLATE_PATH.read_text(encoding="utf-8")

    new_data_js = f"const DATA = {json.dumps(data, ensure_ascii=False, default=str)};"

    # Zamijeni postojeći DATA = {...}; blok
    pattern = r'const DATA = \{.*?\};'
    new_html = re.sub(pattern, new_data_js, html, flags=re.DOTALL)

    if new_html == html:
        # Ako nema pattern, dodaj na kraj skripte
        new_html = html.replace("// ─── Embed data", new_data_js + "\n// ─── Embed data")

    OUTPUT_PATH.write_text(new_html, encoding="utf-8")
    print(f"✅ Dashboard generiran: {OUTPUT_PATH}")
    print(f"   Satna: {len(data['satna'])}, Dnevna: {len(data['dnevna'])}, 15min: {len(data['min15'])}")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    data = export_data(conn)
    conn.close()
    generate_dashboard(data)
