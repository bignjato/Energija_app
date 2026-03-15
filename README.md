# ⚡ Energija App — HEP Energy Monitor

Energetski monitor za HEP ODS kupce sa solarnom elektranom (SMA inverteri).

## Brzi start
```bash
git clone git@github.com:bignjato/Energija_app.git
cd Energija_app
cp .env.example .env
# Uredite .env s vašim podacima
docker-compose up -d
```

Otvorite http://localhost:5000

## Značajke

- 📊 Live dashboard — SMA solar, potrošnja, autarkija
- 🏭 HEP ODS — automatski import mjernih podataka
- ☀️ SMA Sunny Portal — history import (Tripower, Home Manager)
- 🏠 Home Assistant integracija
- 💰 Financijska analiza s projekcijom do kraja mjeseca
- 📈 Optimalno vrijeme potrošnje
- 📋 Pregled i unos računa s usporedbom procjene

## Konfiguracija

Kopirajte `.env.example` u `.env` i popunite podatke:
```env
HEP_USERNAME=vas@email.hr
HEP_PASSWORD=vasa_lozinka
HEP_SIFRA=0149216862
SMA_USERNAME=vas@email.hr
SMA_PASSWORD=vasa_lozinka
...
```

## Licenca

MIT
