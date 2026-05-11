# SMA Monitoring API — setup

Zamjena za Sunny Portal scrape s službenim REST API-jem. Stvarni 5-min podaci za današnji dan + povijesni.

## 1. Registracija na developer.sma.de

1. Idi na **https://developer.sma.de/sma-apis** i prijavi se SMA računom (isti kao Sunny Portal).
2. Pod **My Apps** → **Register new application**:
   - Name: `HEP Energy Monitor` (ili nešto svoje)
   - Description: Personal home energy dashboard
   - Redirect URI: `http://localhost:8080/callback` (privremeno, samo za OAuth flow)
   - Scopes: `monitoring:read`
3. Dobit ćeš **Client ID** + **Client Secret**. Spremi ih.

## 2. Autoriziraj svoju elektranu (OAuth flow — jednom)

SMA koristi Authorization Code grant. Trebaš dobiti **refresh_token** koji potom server koristi.

### 2a. Browser flow (najlakše)

Otvori sljedeći URL u browseru (zamijeni `<CLIENT_ID>` svojim):

```
https://auth.smaapis.de/oauth/authorize?response_type=code&client_id=<CLIENT_ID>&redirect_uri=http://localhost:8080/callback&scope=monitoring:read
```

Prijaviš se SMA računom → odobriš pristup → preusmjeri te na `http://localhost:8080/callback?code=XXX`. **Kopiraj `code`** iz URL-a (samo treba 60s).

### 2b. Zamijeni code za refresh_token

Na Mac-u (treba `curl`):

```bash
curl -X POST https://auth.smaapis.de/oauth/token \
  -u "<CLIENT_ID>:<CLIENT_SECRET>" \
  -d "grant_type=authorization_code" \
  -d "code=<KOPIRANI_CODE>" \
  -d "redirect_uri=http://localhost:8080/callback"
```

Vraća JSON s `access_token` i `refresh_token`. **Spremi `refresh_token`** — taj traje dugotrajno.

## 3. Pronađi `plantId`

```bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" \
     https://monitoring.smaapis.de/v1/plants
```

Vrati će listu — kopiraj `plantId` svoje elektrane.

## 4. Postavi `.env` na serveru

```bash
ssh hep-vps
nano /programi/hep_ha/.env
```

Dodaj:

```
SMA_USE_API=true
SMA_API_CLIENT_ID=tvoj_client_id
SMA_API_CLIENT_SECRET=tvoj_client_secret
SMA_API_REFRESH_TOKEN=tvoj_refresh_token
SMA_API_PLANT_ID=25056   # iz koraka 3
# (opcionalno: SMA_API_BASE_URL, SMA_API_OAUTH_URL — defaulti su OK)
```

## 5. Test

```bash
docker compose restart hep-sync
docker exec hep-energy-sync python /app/sma_api_scraper.py --only recent
```

Trebao bi vidjeti retke poput:
```
SMA: novi access_token (expires_in=3600s)
Recent: spremio sma_live @ 2026-05-11T17:35:00 (PV=8542W feed=7100W)
```

## 6. Provjeri dashboard

Reload `https://energija.infobot.hr` → Pregled tab → "DANAS — SMA" bi sad trebao imati ispravne brojke za sve (predaja, potrošnja kuće, iz mreže).

## Troubleshooting

- **401 Unauthorized**: refresh_token istekao ili pogrešan. Ponovi korak 2.
- **403 Forbidden**: plant owner nije autorizirao tvoju aplikaciju. Provjeri u Sunny Portal-u → Settings → Connected applications.
- **Token rotacija**: ako vidiš "SMA: refresh_token rotiran" u logovima — kopiraj novi iz `docker logs hep-energy-sync` u `.env` i restartaj sync.

## Stari scraper

`sma_scraper.py` (Sunny Portal scrape) ostaje na serveru kao fallback. Ako se vratiš na njega, postavi `SMA_USE_API=false` ili obriši taj redak iz `.env`.
