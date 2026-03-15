# ⚡ Energija App — HEP Energy Monitor

> Energetski monitor za HEP ODS kupce sa solarnom elektranom (SMA inverteri)
> 
> Razvio: **[InfoBot](https://infobot.hr)** — Boris Ignjatović  
> 💻 IT održavanje · 🏠 Pametne kuće · 🖨️ 3D printanje · ✉ boris@infobot.hr

---

## 🚀 Brzi start

```bash
git clone https://github.com/bignjato/Energija_app.git
cd Energija_app
cp .env.example .env
# Uredite .env s vašim podacima
docker-compose up -d
```

Otvorite **http://localhost:5000**

**Prva prijava:** `admin` / `admin`  
⚠️ Promijenite lozinku u Postavke → Korisnici!

---

## ✨ Značajke

| Značajka | Opis |
|----------|------|
| 📊 **Live dashboard** | Trenutna solarna snaga, potrošnja, autarkija |
| 🏭 **HEP ODS** | Automatski import 15-min mjernih podataka |
| ☀️ **SMA Sunny Portal** | History import (Tripower, Home Manager) |
| 🏠 **Home Assistant** | Automatsko slanje senzora u HA Energy |
| 💰 **Financijska analiza** | Procjena računa, projekcija do kraja mj. |
| 📈 **Optimalno vrijeme** | Analiza kada je najpovoljnije trošiti struju |
| 📋 **Upravljanje računima** | Unos stvarnih HEP računa i usporedba |
| 🔄 **Usporedba perioda** | Usporedba dva proizvoljna perioda |
| 🌙 **Dark/Light mode** | Prilagodba sučelja |
| 🔒 **Višekorisnički pristup** | Admin i Viewer uloge s hashiranim lozinkama |
| 💾 **Backup baze** | Automatski dnevni backup + ručni download |

---

## 📋 Preduvjeti

- **Docker** i **Docker Compose**
- **HEP ODS** korisnički račun (mjerenja.hep.hr)
- **SMA Sunny Portal / Ennexos** račun *(opcionalno)*
- **Home Assistant** 2023.x+ *(opcionalno)*

---

## ⚙️ Konfiguracija

Kopirajte `.env.example` u `.env` i popunite:

```env
# HEP ODS — obavezno
HEP_USERNAME=vas@email.hr
HEP_PASSWORD=vasa_lozinka
HEP_SIFRA=vasa_sifra_mjernog_mjesta

# SMA Sunny Portal — opcionalno
SMA_USERNAME=vas@email.hr
SMA_PASSWORD=vasa_lozinka
SMA_PLANT_ID=
SMA_INV1_ID=
SMA_INV2_ID=

# Home Assistant — opcionalno
HA_URL=https://homeassistant.local:8123
HA_TOKEN=eyJ...
```

Ili koristite **Setup Wizard** na `/setup` pri prvom pokretanju.

Konfiguracija se čuva u SQLite bazi — `.env` je samo za inicijalizaciju.

---

## 🏗️ Arhitektura

```
Energija_app/
├── app.py                  # Flask server + svi API endpointi
├── hep_scraper.py          # HEP ODS scraper (JWT auth)
├── sma_scraper.py          # SMA live podaci (svakih 5 min)
├── sma_history_import.py   # SMA history import (Max aggregate)
├── ha_sender.py            # Home Assistant sender
├── sync_loop.sh            # Orchestrator (5min/1h/24h raspored)
├── dashboard_template.html # Frontend dashboard (vanilla JS)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## 🔄 Sync raspored

| Što | Kada |
|-----|------|
| SMA live podaci | Svakih **5 minuta** |
| HEP ODS podaci | Svakih **sat vremena** |
| Home Assistant sync | Svakih **sat vremena** |
| SMA history import | Jednom **dnevno** |
| Backup baze | Jednom **dnevno** (zadnjih 7) |

---

## 🛡️ Sigurnost

- Lozinke se čuvaju kao `salt:SHA256(salt:password)` — nije reverzibilno
- Zadnji admin ne može se obrisati
- Sve rute zaštićene loginom
- `.env` i baza su **izvan Git repozitorija**

---

## 🚢 Deploy na produkciju

```bash
# VPS s nginx + Let's Encrypt SSL
apt install nginx certbot python3-certbot-nginx
certbot --nginx -d vasa-domena.hr

# Nginx reverse proxy na port 5000
# Pogledajte nginx-hep.conf za primjer konfiguracije
```

---

## 🤝 Kompatibilnost

- **HEP ODS** — svi kupci s pametnim brojilom (mjerenja.hep.hr)
- **SMA inverteri** — Sunny Tripower, Sunny Boy (Sunny Portal/Ennexos)
- **SMA client_id** — `SPpbeOS` (reverse engineered, može se promijeniti)
- **Home Assistant** — 2023.x i noviji

---

## 📄 Licenca

MIT License — slobodno koristite, modificirajte i distribuirajte.

---

## 👨‍💻 Autor

**InfoBot** — Obrt za informatičke i druge usluge  
vl. Boris Ignjatović

- 🌐 [infobot.hr](https://infobot.hr)
- ✉️ boris@infobot.hr
- 💻 IT održavanje
- 🏠 Pametne kuće  
- 🖨️ 3D printanje

---

*Razvijeno za praćenje solarne elektrane 30 kWp + HEP ODS mjerač u Lukavcu, Hrvatska*
