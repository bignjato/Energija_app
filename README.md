# HEP Energy Monitor

Open-source Flask dashboard za korisnike **HEP ODS-a** u Hrvatskoj. Scrape-a 15-minutna mjerenja s portala `mjerenje.hep.hr`, opcionalno integrira **SMA Sunny Portal** (solar) i **Home Assistant** (push senzora).

Funkcionalnosti:
- 📊 Dashboard s potrošnjom i predajom u mrežu (satno / dnevno / mjesečno)
- 📄 Upload HEP PDF računa s automatskim parsiranjem (admin-only)
- 💰 Procjena mjesečnog računa po stvarnim cijenama HEP-a + VT/NT slider
- ⚡ Vršna snaga (kW) iz 15-min očitanja
- 🎯 Auto-kalibracija VT/NT udjela iz vlastitih satnih podataka
- ☀️ Integracija SMA Sunny Portal (PV inverter)
- 🏠 Push senzora u Home Assistant
- 🔔 HA alerti: stale podaci, anomalija potrošnje, podsjetnik za upload računa
- 🔒 Login + admin/viewer uloge, PBKDF2 hashing, CSRF, rate-limit, HTTPS-ready

## Brzi start (Docker)

Treba ti Docker, Docker Compose, i HEP ODS pristup ([mjerenje.hep.hr](https://mjerenje.hep.hr)).

```bash
git clone git@github.com:bignjato/Energija_app.git
cd Energija_app
cp .env.example .env

# Generiraj SECRET_KEY
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env

nano .env   # popuni HEP_USERNAME, HEP_PASSWORD, HEP_SIFRA

docker compose up -d --build
```

Otvori http://localhost:5000 → prvi login `admin / admin` (ili vrijednost iz `INITIAL_ADMIN_PASSWORD`) → **Setup wizard** vodi te kroz konfiguraciju → promijeni admin lozinku u Postavkama.

## Arhitektura

Dva Docker servisa iz iste slike:

| Servis | Komanda | Uloga |
|---|---|---|
| `hep-web` | `gunicorn app:app` | Flask web/API na :5000 |
| `hep-sync` | `sh /app/sync_loop.sh` | Scraper petlja (5min ciklus) |

Volume `hep-data` mountan na `/data` (SQLite + dnevni backupi).

### Struktura kôda

```
hepapp/
├── __init__.py        — Flask app factory
├── core.py            — auth decorators, password hashing, CSRF, rate-limit
├── db.py              — SQLite, init, WAL mode
├── tariff.py          — HEP tarife, izračun računa
├── auth.py            — login/logout (BRAND_* env)
├── bill_parser.py     — HEP PDF parser (pdfplumber + regex)
├── static/            — dashboard.css + dashboard.js (cache-busting preko ?v=)
└── blueprints/
    ├── views.py       — / + /health
    ├── data.py        — /api/data, povijest, sma/live
    ├── stats.py       — /api/stats/* (peak, kalibracija, VT/NT trošak)
    ├── tarifa.py      — /api/tarifa
    ├── racuni.py      — /api/racuni + PDF upload
    ├── postavke.py    — /api/postavke + korisnici + backup
    ├── setup.py       — /api/setup/* (manual scraper trigger)
    └── wizard.py      — /setup (first-run wizard)

hep_scraper.py         — HEP ODS scraper (15-min krivulje A+/A-)
sma_scraper.py         — SMA Sunny Portal live scraper
sma_history_import.py  — SMA history backfill
ha_sender.py           — Home Assistant senzor push
netutil.py             — HTTP retry/backoff + HA_VERIFY_SSL helper
maintenance.py         — backfill, retencija, alerti (stale/anomalija/računi),
                         restore-drill, off-site i token-age provjere
sync_loop.sh           — orkestracija (SMA 5min, HEP+HA 1h, history 24h)
offsite_backup.sh      — opcionalan upload na rclone/rsync remote
tests/                 — pytest (bill parser, tarife, scraper, maintenance)
```

## Konfiguracija

`.env` se učitava pri startu, kasnije se postavke (osim `SECRET_KEY`) mijenjaju kroz **Postavke** tab u UI-u (sprema se u SQLite + nazad u `.env`).

Ključne varijable:
- `HEP_USERNAME` / `HEP_PASSWORD` / `HEP_SIFRA` — credentialsi za HEP ODS
- `SECRET_KEY` — **OBAVEZNO**, 32+ char random hex
- `INITIAL_ADMIN_PASSWORD` — lozinka prvog admin korisnika (default `admin`, promijeni je odmah)
- `BRAND_NAME` / `BRAND_OWNER` / `BRAND_EMAIL` / `BRAND_PHONE` / `BRAND_WEB` — opcionalno, prikazuju se u header-u
- `OFFSITE_BACKUP_MODE` / `OFFSITE_BACKUP_DEST` — off-site backup (vidi `ONEDRIVE_SETUP.md`)

Sve ostalo: vidi `.env.example`.

## Nginx + TLS

`nginx-example.conf` ima primjer s Let's Encrypt SSL, HSTS i security headerima. Reverse proxy na `localhost:5000`. Prilagodi `server_name` i Let's Encrypt putanju za svoju domenu.

## Off-site backup

Dnevni lokalni backup baze u `/data/backups/` (retencija 7). Za off-site (OneDrive / B2 / S3 / NAS) vidi `ONEDRIVE_SETUP.md` — postavi `OFFSITE_BACKUP_MODE=rclone` i konfiguriraj rclone remote.

## API endpoint-i

| Method | Path | Auth |
|---|---|---|
| GET | `/health` | — |
| GET | `/api/data` | login |
| GET | `/api/data/sve` | login |
| GET | `/api/povijest?od=YYYY-MM-DD&do=YYYY-MM-DD&res=day\|hour\|week` | login |
| GET | `/api/sma/live` | login |
| GET | `/api/stats/usporedba` | login |
| GET | `/api/stats/optimalno` | login |
| GET | `/api/stats/mjesecni` | login |
| GET | `/api/stats/peak-snaga` | login |
| GET | `/api/stats/vt-kalibracija?mjesec=YYYY-MM` | login |
| GET | `/api/stats/vt-nt-trosak` | login |
| GET/POST | `/api/tarifa` | login (POST: admin) |
| GET/POST/DELETE | `/api/racuni` | login (POST/DELETE: admin) |
| POST | `/api/racuni/upload-pdf` | admin |
| GET/POST | `/api/postavke` | admin |
| GET | `/api/postavke/status` | login |
| GET | `/api/postavke/mjerno-mjesto` | login |
| GET | `/api/postavke/backup` | admin |
| GET/POST/DELETE | `/api/postavke/korisnici` | admin |
| POST | `/api/setup/import-hep` (`?dani=N`) | admin |
| POST | `/api/setup/import-sma` | admin |
| POST | `/api/setup/sync-ha` | admin |

## Sigurnost

- PBKDF2-SHA256 password hashing (200k iter)
- Session cookies: Secure, HttpOnly, SameSite=Lax
- CSRF: per-session token (X-CSRF-Token) + Origin check na POST/PUT/DELETE
- Rate limit 10/min, 30/h na `/login` (flask-limiter)
- HSTS + X-Frame-Options + X-Content-Type-Options + Referrer-Policy preko nginx-a
- SSL verifikacija prema Home Assistantu (isključivo s `HA_VERIFY_SSL=0` za self-signed)
- SQLite WAL mode + busy_timeout 5s; dnevni backup preko `sqlite3 .backup` (WAL-safe)
- Non-root user (`app`) u kontejneru

Nedostaje: 2FA/TOTP, audit log, K8s deployment manifest.

## Testovi

```bash
pip install -r requirements-dev.txt
python -m pytest tests/

# ili bez lokalnog Pythona:
docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  sh -c "pip install -q flask requests pytest && python -m pytest tests/ -q"
```

## Tehnologije

- Python 3.12, Flask 3.1, Gunicorn
- SQLite (WAL mode)
- pdfplumber za HEP PDF parsing
- Chart.js za grafove
- nginx za TLS termination
- Docker / Docker Compose

## License

MIT — slobodno koristi i prilagodi.

Pull requesti dobrodošli. Ako trebaš pomoć s deployment-om, otvori issue.
