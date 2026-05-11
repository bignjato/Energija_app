# Home Assistant pull — instant solarni podaci (preporučeno)

> **Brza alternativa SMA Monitoring API-ju**. Tvoj HA već lokalno (preko
> Modbusa) razgovara sa SMA Sunny Home Managerom / inverterom i ima sve
> live brojke kao senzore. Mi ih samo čitamo preko HA REST API-ja —
> bez SMA registracije, bez čekanja, bez OAuth dance.

## Preduvjeti

- Home Assistant već povezan sa SMA preko Modbusa (vjerojatno već imaš)
- HA dostupan iz Docker kontejnera (preko VPN / Nabu Casa / public URL)
- HA Long-Lived Access Token (već u `.env` kao `HA_TOKEN`)

## 1. Pronađi entity_id-eve SMA senzora u Home Assistantu

HA → Developer Tools → States → filtriraj "sma" ili "sunny". Tipično:

| Što mjeri | Tipičan entity_id |
|---|---|
| Solar power (W) | `sensor.sma_pv_power` ili `sensor.sma_total_generation` |
| Feed-in power (W) | `sensor.sma_grid_feed_in_power` |
| Total consumption (W) | `sensor.sma_total_consumption` |
| Grid consumption (W) | `sensor.sma_grid_consumption` |
| Autarky (%) | `sensor.sma_autarky` |
| Battery SoC (%) | `sensor.sma_battery_soc` |
| Solar today (kWh) | `sensor.sma_yield_today` |
| Feed-in today (kWh) | `sensor.sma_grid_feed_in_today` |
| Consumption today (kWh) | `sensor.sma_consumption_today` |
| Grid today (kWh) | `sensor.sma_grid_consumption_today` |

**Kopiraj točan entity_id** svakog senzora — vlastiti.

## 2. Provjeri da Token radi i HA dostupan

Iz kontejnera:
```bash
docker exec hep-energy-sync curl -k -H "Authorization: Bearer $HA_TOKEN" \
  "$HA_URL/api/states/sensor.sma_pv_power"
```

Trebaš dobiti JSON s `"state": "8542"` (ili nešto slično u W).

Ako vraća 401 — HA_TOKEN je krivi. Ako "connection refused" — HA_URL nije dostupan iz kontejnera (riješi VPN / port-forward / Nabu Casa).

## 3. Postavi entity_id-eve u `.env`

```bash
ssh hep-vps
nano /programi/hep_ha/.env
```

Dodaj na kraj (zamijeni stvarnim entity_id-ovima):

```
# Home Assistant pull (alternativa SMA Monitoring API)
SMA_USE_HA_PULL=true
HA_ENT_PV_W=sensor.sma_pv_power
HA_ENT_FEED_W=sensor.sma_grid_feed_in_power
HA_ENT_CONS_W=sensor.sma_total_consumption
HA_ENT_GRID_W=sensor.sma_grid_consumption
HA_ENT_AUTARKY=sensor.sma_autarky
HA_ENT_BATTERY_SOC=sensor.sma_battery_soc
# Dnevni kWh (opcionalno, ako ih imaš):
HA_ENT_PV_KWH_DAY=sensor.sma_yield_today
HA_ENT_FEED_KWH_DAY=sensor.sma_grid_feed_in_today
HA_ENT_CONS_KWH_DAY=sensor.sma_consumption_today
HA_ENT_GRID_KWH_DAY=sensor.sma_grid_consumption_today
```

## 4. Restart

```bash
cd /programi/hep_ha && docker compose restart hep-sync
```

## 5. Test

```bash
docker exec hep-energy-sync python -m hepapp.ha_puller --only recent
docker exec hep-energy-sync python -m hepapp.ha_puller --only today
```

Treba pokazati:
```
HA pull: PV=8542W feed=7100W cons=1442W grid=0W aut=1.0 soc=85
HA pull today: PV=86.4 kWh, feed=82.1 kWh, cons=4.3 kWh, grid=0.0 kWh
```

## 6. Provjeri dashboard

Reload `https://energija.infobot.hr` → Pregled tab → "DANAS — SMA" pokazuje stvarne brojke za predaju i potrošnju.

## Troubleshooting

- **`null` u sma_live**: HA entity vraća `unknown` ili `unavailable` — provjeri u HA da li senzor radi.
- **Connection refused**: kontejner ne vidi HA_URL. Ako ti je HA na lokalnoj mreži, koristi Nabu Casa (`https://xxx.ui.nabu.casa`) ili dynamic DNS.
- **HTTPS sa self-signed certom**: `ha_puller.py` koristi `verify=False` pa ovo nije problem.

## Sync loop integration

`sync_loop.sh` možeš modificirati da pokreće HA puller umjesto SMA scrapera kad
je `SMA_USE_HA_PULL=true`. Postojeća logika feature-flag-a obrađuje to.
