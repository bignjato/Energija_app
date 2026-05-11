# SMA Monitoring API — setup

> **Napomena**: SMA NE nudi self-service registraciju. Client credentialse moraš
> zatražiti **manualno** od SMA tima. Proces traje **nekoliko dana** dok ne
> odobre. Ako trebaš brže rješenje, vidi `HA_PULL_SETUP.md` (čitanje preko
> Home Assistant-a — radi instantno).

Reference: https://developer.sma.de/api-access-control

## 1. Pripremi materijale za prijavu

SMA ti šalje OAuth client_id/secret **samo nakon što im pošalješ**:
- URL na logo aplikacije (`yourApp.com/assets/logo.png`)
- URL na Terms of Service (`yourApp.com/serviceTerms`)
- URL na Privacy Policy (`yourApp.com/dataPrivacy`)
- **Redirect URI** za Code Grant Flow (npr. `https://energija.example.com/sma/callback`)

Za hobi/personal projekte to možeš parkirati na svom GitHub Pages ili statickoj
stranici. Logo treba postojati na javnom URL-u.

## 2. Kontaktiraj SMA

Idi na **https://developer.sma.de/contact** i pošalji im upit:
- Tip aplikacije: O&M / Backend monitoring
- Opis use-case-a: Personal home energy dashboard
- Linkovi iz koraka 1
- Da li želiš Code Grant Flow ili Custom Flow (preporučujem **Code Grant** za
  personal dashboard — jednostavnije)

Pričekaj odgovor — može trajati dane do tjedana. Dobit ćeš:
- **Client ID**
- **Client Secret**

## 3. OAuth Authorization Code Flow (jednom)

Plant-owner (ti) mora autorizirati aplikaciju. To je 2-korak browser flow:

### Step 1: Authorization request

Otvori u browseru:
```
https://auth.smaapis.de/oauth2/auth?response_type=code&client_id=<CLIENT_ID>&redirect_uri=<REDIRECT_URI>&state=random123
```

Prijavi se SMA Sunny Portal računom → odobri pristup. Browser te preusmjeri na:
```
<REDIRECT_URI>?code=SplxlOBeZQQYbYS6WxSbIA&state=random123
```

**Kopiraj `code`** iz URL-a (vrijedi ~60 sekundi).

### Step 2: Exchange code for tokens

```bash
curl -X POST https://auth.smaapis.de/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=<CLIENT_ID>" \
  -d "client_secret=<CLIENT_SECRET>" \
  -d "grant_type=authorization_code" \
  -d "code=<CODE_IZ_STEP_1>" \
  -d "redirect_uri=<REDIRECT_URI>"
```

Odgovor:
```json
{
  "access_token": "eyJ...",
  "refresh_token": "abc...",
  "expires_in": 3600
}
```

**Spremi `refresh_token`** — taj traje dugotrajno, koristi se za auto-obnavljanje access tokena.

## 4. Pronađi plant ID

```bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" \
     https://monitoring.smaapis.de/v1/plants
```

Vraća listu — kopiraj `plantId` svoje elektrane.

## 5. Postavi `.env` na serveru

```bash
ssh hep-vps
nano /programi/hep_ha/.env
```

```
SMA_USE_API=true
SMA_API_CLIENT_ID=<od SMA>
SMA_API_CLIENT_SECRET=<od SMA>
SMA_API_REFRESH_TOKEN=<iz step 3>
SMA_API_PLANT_ID=<iz step 4>
```

## 6. Test

```bash
docker compose restart hep-sync
docker exec hep-energy-sync python /app/sma_api_scraper.py --only recent
```

Trebao bi vidjeti:
```
SMA: novi access_token (expires_in=3600s)
Recent: spremio sma_live @ 2026-05-11T17:35 (PV=8542W feed=7100W)
```

## Troubleshooting

- **401 Unauthorized**: refresh_token istekao. Ponovi korak 3.
- **403 Forbidden**: plant owner nije autorizirao aplikaciju. Provjeri Sunny Portal → Settings → Connected applications.
- **Token rotacija**: ako vidiš "SMA: refresh_token rotiran" u logovima — kopiraj novi iz `docker logs hep-energy-sync` u `.env` i restartaj.
- **Sandbox**: za testiranje prije produkcijskih credentialsa, koristi `SMA_API_BASE_URL=https://sandbox.smaapis.de/monitoring` + `SMA_API_TOKEN_URL=https://sandbox-auth.smaapis.de/oauth2/token`. SMA Sandbox accounts: `apiTestUser@apiSandbox.com / MyPass123!`.

## Stari Sunny Portal scraper

Ako `SMA_USE_API=false` (default), koristi se `sma_scraper.py` (legacy Sunny Portal scrape).
