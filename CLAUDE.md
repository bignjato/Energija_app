# HEP_HA — Energy Dashboard (Claude project notes)

Flask aplikacija + scraperi za HEP ODS (potrošnja) i SMA Sunny Portal (proizvodnja), s push-em u Home Assistant.

## Arhitektura

Dva docker kontejnera (docker-compose), oba iz iste slike:

| Kontejner | Komanda | Uloga |
|---|---|---|
| `hep-energy-web` | `gunicorn app:app` | Flask web/API na :5000 (non-root) |
| `hep-energy-sync` | `sh /app/sync_loop.sh` | Scraper petlja |

Volume `hep-data` → `/data/hep_energy.db` (SQLite + WAL) + `/data/backups/` (lokalni dnevni backupi).

Nginx (host) reverse-proxy na `:5000` s Let's Encrypt + HSTS + sec headerima.

## Komponente

Vidi `README.md` za detaljni layout (`hepapp/` paket s blueprints, scraperi u root-u).

## Konfiguracija

`.env` u repo root-u (gitignore-an). Pri prvom bootu `init_db` migrira ENV ključeve u SQLite `config` tablicu (`_migrated` flag). Nakon toga postavke se mijenjaju kroz Postavke tab u UI-u.

Setup wizard se aktivira samo na čistim instalacijama (postojeće — s users + HEP_USERNAME — auto-označene kao gotove).

## Komande

```bash
# Deploy nakon git pull
cd /programi/hep_ha
git pull origin main
docker compose up -d --build

# Logovi
docker logs -f hep-energy-web
docker logs -f hep-energy-sync
tail -f /data/sync.log

# Manualni scrape
docker exec hep-energy-sync python /app/hep_scraper.py --dani 7

# DB shell
docker exec -it hep-energy-web sqlite3 /data/hep_energy.db

# Testovi (lokalno nema pip-a — koristi container)
docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  sh -c "pip install -q flask requests pytest && python -m pytest tests/ -q"
```

## Sigurnosne note

- Nikad ne stavljaj `.env`, `env`, ili bilo koji file s lozinkama/tokenima u git
  (oboje su u `.gitignore`).
- SECRET_KEY rotacija invalidira sve aktivne sesije — postavi jednom i drži.
- Default admin lozinka `admin` mora se promijeniti odmah nakon prvog logina.
- HEP_TOKEN / HA_TOKEN tokeni stari ≥90 dana — povremeno regeneriraj.
- `HA_VERIFY_SSL` default je uključen — ako HA ima self-signed cert, postavi
  `HA_VERIFY_SSL=0` u `.env` (inače HA push/pull pada na SSL grešci).

## Mogući sljedeći koraci

- 2FA / TOTP za admin
- Više brojila / multi-OMM selektor u UI
- Cumulative kWh chart u mjesecu
- K8s deployment manifest
