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
./deploy.sh

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

## Konvencije podataka (provjereno prema brojilu i računima — ne mijenjati napamet)

- **Vremenska zona**: oba kontejnera rade s `TZ=Europe/Zagreb`. Python `datetime.now()` je lokalno
  vrijeme; SQLite `date('now')`/`datetime('now')` su i dalje UTC — za lokalno koristi modifikator
  `'localtime'` ili pomoćne funkcije iz `hepapp/timeutil.py`.
- **Formati vremena u bazi**:
  - HEP (`ocitanja_15min/satna.ts`, `ocitanja_dnevna.datum`): lokalno, bez offseta, `YYYY-MM-DDTHH:MM:SS`
  - `sma_live.ts`: UTC s offsetom (`...+00:00`); `sma_15min.ts`: UTC sa `Z`; `sma_dnevna.datum`: lokalni dan
  - `sync_log.ts`: UTC s razmakom (`YYYY-MM-DD HH:MM:SS`)
  - Nikad ne uspoređuj `T`-format s `datetime('now')` (leksički `'T' > ' '` pa cijeli današnji dan ispadne).
- **HEP 15-min uzorak nosi oznaku KRAJA intervala**: uzorak `T` pokriva `(T-15min, T]`. Satna/dnevna
  agregacija pripisuje ga početku intervala (`ts, '-15 minutes'`). S tim se mjesečne sume poklapaju s
  registrima brojila do ~0,3 kWh.
- **VT/NT**: VT je 07–21 zimi i 08–22 ljeti (= 06–20 UTC cijele godine). `TARIFA_VT_OD/DO` su zimski sati.
  Klasifikacija ide kroz `timeutil` (ne fiksni lokalni sati).
- **Cijene**: `hepapp/tariff.py` — `get_tarifa(datum)` kreće od ugrađenog cjenika po datumu važenja
  (`TARIFE`) i prekriva ga jediničnim cijenama sa zadnjeg uvezenog računa (`racuni.cijene_json`,
  parser ih čita iz PDF-a). Povijesni mjeseci se računaju po tadašnjem cjeniku (`datum=`).
  `tests/test_tariff.py` drži regresiju protiv stvarnih računa (±5 centi).
- **`sma_dnevna` ima više pisača** (HA puller = današnji red; `sma_history_import` = samo PV kolone;
  `maintenance.backfill_daily` = popunjava NULL-ove iz `sma_live`). Nikad `INSERT OR REPLACE` u tu
  tablicu — briše tuđe kolone. Samo `ON CONFLICT(datum) DO UPDATE` vlastitih kolona.
- **Deploy**: samo `./deploy.sh` (git pull → build → up -d → čeka healthy). Ne `docker cp` u kontejner
  (gubi se pri recreate). Web port je vezan na loopback; javno ide isključivo kroz nginx.
- **Poznata ograničenja**: sat koji se ponavlja pri prelasku na zimsko vrijeme sudara se na
  `UNIQUE(mjerno_mjesto, ts)` (~5 kWh/god fali u dnevnim sumama); postavke koje UI piše u `.env`
  unutar web kontejnera ne dolaze do sync kontejnera (vrijedi serverski `.env`).

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
