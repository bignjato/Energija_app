# Primopredaja — stanje projekta (2026-07-14)

Repo je na `main`, sve pushano (zadnji commit `851a829`, sinkronizirano s
`origin/main`). Produkcija na 89.117.54.89 (SSH port 2244, alias `ssh vps`) —
Ubuntu VM, docker compose u `/programi/hep_ha`, kontejneri `hep-energy-web`
(:5000, javno https://energija.infobot.hr) i `hep-energy-sync`. SQLite baza:
volume `/data/hep_energy.db` (+ `/data/backups/` lokalni dnevni backupi).

## Što je napravljeno od zadnje primopredaje (2026-06-12 → danas)
- **maintenance robustnost** — restore-drill (test obnove backupa), off-site
  health-check, token-age provjera (HEP/HA tokeni ≥90 d), anomalija potrošnje
  na rolling baseline, WAL-safe backup, retencija. Sve uvezano u `sync_loop.sh`.
- **schema versioning** — `schema_version` tablica gate-a migracije; datum
  tajni za računanje starosti tokena (`066ae2f`).
- **PDF parser upozorenja** — parser sada zapisuje `napomena` upozorenje ako
  naiđe na neprepoznata polja u računu (`96da82c`).
- **multi-OMM** — selektor mjernog mjesta u UI + kumulativni kWh graf mjeseca.
- **k8s** — deployment manifesti (web+sync u jednom podu, PVC, ingress,
  kustomize) u `k8s/`.
- **testovi** — pytest suite narastao na ~57 testova (maintenance backup/
  offsite/token/anomaly, SMA API, stats, parser, tarifa).
- **razne ispravke** — HTTP retry+backoff, `HA_VERIFY_SSL` flag, CSRF token,
  timezone-aware datetime u `ha_puller`, CSS/JS izdvojen u `static/`, frontend
  učitava plant-info/cijene/weather/power-limit na boot.

## Računi / dug (ažurirano 2026-07-14)
- **6 PDF računa** u tablici `racuni`: 2026-01 … 2026-06. Zadnji dodan
  2026-06 (id 8, unesen 2026-07-13) parsiran čisto — sve stavke se poklapaju
  (opskrba+mreza+pdv = iznos, kwh_vt+kwh_nt = kwh_plus), bez upozorenja.
- **Projekcija duga** (`/api/stats/dug`) sada koristi 5 računa s dugom
  (2026-03 nema dug — stariji ručni unos). Dug pada konzistentno ~−144 €/mj
  zadnja 3 mjeseca (1074 → 933 → 790 → 641). Uz tempo, otplata oko
  listopada/studenog 2026. Model tarife: HEPI bijeli (net-metering), projekcija
  isplate iz delta registara (`hep_registri`).

## Poznata ograničenja / sljedeći koraci
- `sma_dnevna` flow-metrike (feed/grid/cons/autarkija) postoje tek od
  2026-03-15 (ranije `sma_live` nije imao te senzore) — nepopravljivo.
- Više unesenih PDF računa → bolja projekcija duga i isplate.
- Deploy konvencija: `cd /programi/hep_ha && git pull && docker compose up -d
  --build`. Ne koristiti `docker cp` (non-root kontejner, problemi s dozvolama).
- `.env` na serveru drži tajne (HEP/SMA/HA kredencijali) — nije u gitu.
- TODO za vlasnika: rotirati root SSH lozinku servera; rotirati HEP/HA tokene
  ako maintenance token-age provjera javi ≥90 dana.
