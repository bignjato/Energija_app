# HEP_HA — Energy Dashboard

Flask aplikacija + scraperi za HEP (potrošnja) i SMA Sunny Portal (proizvodnja), s push-em u Home Assistant. Dashboard na **energija.infobot.hr**.

Autor: InfoBot — Boris Ignjatović

## Server

- **Host**: `hep-vps` (89.117.54.89:2244, root) — isti server kao `servis.onetech.hr`
- **Putanja**: `/programi/hep_ha`
- **Git**: deploy preko `git pull origin main` + `deploy.sh`
- **SSH ključ**: `~/.ssh/hep_vps` (lokalno na Mac); na serveru `/root/.ssh/github_hep` za GitHub

## Arhitektura

Dva docker kontejnera (docker-compose), oba iz iste slike:

| Kontejner | Komanda | Uloga |
|---|---|---|
| `hep-energy-web` | `python app.py` | Flask web/API na :5000 |
| `hep-energy-sync` | `sh /app/sync_loop.sh` | Scraper petlja (5min ciklus) |

Volume: `hep-data` → `/data/hep_energy.db` (SQLite, ~37 MB) + `/data/backups/` (zadnjih 7).

Nginx (host) → `:5000`, Let's Encrypt na `energija.infobot.hr`.

## Komponente

- `app.py` (1117 redaka) — Flask API + auth + postavke + statistike. SQLite, sesije, hash login.
- `dashboard_template.html` (82 KB) — jedinstveni frontend (vanilla, jedan file).
- `hep_scraper.py` — scrape HEP MojRačun (potrošnja po satu, VT/NT).
- `sma_scraper.py` — Sunny Portal trenutna proizvodnja (svakih 5 min).
- `sma_history_import.py` — povijesni SMA import (1×/dan).
- `ha_sender.py` — push senzori u Home Assistant preko REST API-ja.
- `generate_dashboard.py`, `sync_all.py` — pomoćne skripte.
- `sync_loop.sh` — orkestracija: SMA 5min, HEP+HA 1h, SMA history 24h, backup 02:00.

## Konfiguracija

`.env` u `/programi/hep_ha/.env`. Pri prvom bootu `app.py` migrira ENV ključeve u SQLite `config` tablicu (`_migrated` flag). Nakon toga postavke se mijenjaju kroz UI, ne kroz `.env`.

Tarife (HRK→EUR): VT 0.131205, NT 0.064379, PROD 0.064379, PDV 13%, VT prozor 07–21h.

Defaultni login: `admin/admin` (kreira se ako nema korisnika).

## Trenutno poznato stanje (2026-05-11)

- Oba kontejnera prijavljuju **`unhealthy`** ali rade ispravno. Razlog: Dockerfile koristi `wget` u HEALTHCHECK, ali `python:3.12-slim` nema `wget`. Aplikacija servira `/health` normalno.
- `app.py` se pokreće **Flask dev serverom**, iako je `gunicorn` u `requirements.txt` (neiskorišten).
- `sqlite3` CLI nije u kontejneru (otežava debugging baze).
- Oba servisa grade istu sliku → 2× build vremena; mogu dijeliti image.
- `dashboard_template.html` je 82 KB monolit.

## Optimizacijski plan (kandidati)

1. **Healthcheck**: zamijeniti `wget` s `python -c 'import urllib.request; urllib.request.urlopen("http://localhost:5000/health")'` ili `curl` s instalacijom. Brzo & sigurno.
2. **Gunicorn**: prebaciti web kontejner na `gunicorn -w 2 -b 0.0.0.0:5000 app:app`. Dev server nije za produkciju.
3. **Image reuse**: `hep-sync` neka koristi `image:` od `hep-web`-a umjesto vlastitog builda.
4. **`sqlite3` CLI** u image (`apt-get install -y --no-install-recommends sqlite3`).
5. **Dockerfile**: `COPY . .` umjesto popisivanja svakog file-a (ili `.dockerignore`); slojevi za caching.
6. **WAL mode** na SQLite za bolji concurrent read tijekom scrape pisanja.
7. **app.py refaktor**: izvući rute u blueprints (`auth`, `api`, `postavke`, `stats`) — sad je 1117 redaka u jednom file-u.
8. **Logiranje**: standardizirati na `logging` umjesto `print` + Flask dev logs; rotate.
9. **Backup retencija**: trenutno 7 dnevnih lokalno — razmotriti off-site (rsync/B2/S3).
10. **Frontend**: razdvojiti `dashboard_template.html` na assets + cache headers već postoje u nginx-u za js/css/png/ico.

## Komande

```bash
# Spoji se
ssh hep-vps

# Deploy
cd /programi/hep_ha && ./deploy.sh

# Logovi
docker logs -f hep-energy-web
docker logs -f hep-energy-sync

# Manualni scrape
docker exec hep-energy-sync python /app/hep_scraper.py --dani 7
docker exec hep-energy-sync python /app/sma_scraper.py

# DB shell (nakon dodavanja sqlite3 u image)
docker exec -it hep-energy-web sqlite3 /data/hep_energy.db
```
