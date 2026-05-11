# OneDrive backup setup (rclone)

OneDrive auth zahtijeva interaktivni OAuth flow s browserom. Korake odradiš jednom; nakon toga sync kontejner automatski uploada svaki dnevni backup u 02:00.

## 1. Autorizacija (radi se SAMO jednom, na Mac-u)

Na Mac-u (gdje imaš browser i konekciju na M365 račun):

```bash
brew install rclone
rclone authorize "onedrive"
```

To otvori browser → prijaviš se na svoj OneDrive (Microsoft) → klikneš "Allow" → rclone u terminalu ispiše JSON token (`{"access_token":"…","expiry":"…"}`). **Kopiraj cijeli taj JSON** (uključujući zagrade).

## 2. Konfiguracija na serveru

```bash
ssh hep-vps
rclone config
```

Interaktivno:
- `n` → new remote
- name: `onedrive`
- Storage: `onedrive` (broj iz liste, npr. 27)
- `client_id`: ostavi prazno (default)
- `client_secret`: ostavi prazno
- `region`: `1` (global)
- `Edit advanced config?`: `n`
- `Use auto config?`: **`n`** (jer si već autorizirao na Mac-u)
- `result>`: paste JSON token iz koraka 1
- `Choose drive type`: `1` (OneDrive Personal) ili `2` (Business/SharePoint)
- `Chose drive to use`: potvrdi default
- `Is that okay?`: `y` → quit

Test:
```bash
rclone lsd onedrive:
rclone mkdir onedrive:hep-backups
rclone copy /data/backups/hep_energy_*.db onedrive:hep-backups/  # ako ima lokalnih backupa
```

## 3. Premjesti rclone config u shared volume (container vidi ga)

```bash
mkdir -p /programi/hep_ha/rclone-config
cp ~/.config/rclone/rclone.conf /programi/hep_ha/rclone-config/rclone.conf
chmod 600 /programi/hep_ha/rclone-config/rclone.conf
```

## 4. Update `docker-compose.yml` da mountamo config + env

Dodaj u oba `hep-web` i `hep-sync` servisa:

```yaml
    volumes:
      - hep-data:/data
      - ./rclone-config:/home/app/.config/rclone:ro   # ← novo
    environment:
      - DB_PATH=/data/hep_energy.db
      - OFFSITE_BACKUP_MODE=rclone
      - OFFSITE_BACKUP_DEST=onedrive:hep-backups/
      - OFFSITE_BACKUP_KEEP=30
```

## 5. Restart

```bash
cd /programi/hep_ha && docker compose up -d
```

Provjeri da kontejner vidi remote:
```bash
docker exec hep-energy-sync rclone lsd onedrive:
```

## 6. Manualni test

```bash
docker exec hep-energy-sync /bin/sh /app/offsite_backup.sh
tail -30 /data/sync.log
```

Trebao bi vidjeti retke poput:
```
[offsite] rclone copy /data/backups/hep_energy_20260511_0200.db → onedrive:hep-backups/
[offsite] OK rclone upload
```

## 7. Provjera s OneDrive web sučelja

Otvori onedrive.live.com → mapa **hep-backups** → datoteke se pojavljuju nakon 02:00.

Retencija: stari backupi >30 dana brišu se automatski (`rclone delete --min-age 30d`).

---

## Troubleshooting

- **Token istekao**: rclone OneDrive token traje ~90 dana. Ako vidiš "401 Unauthorized" u `/data/sync.log`, ponovi korak 1 i 2.
- **"failed to authenticate"**: provjeri da je `rclone.conf` chmod 600 i u read-only mount-u.
- **Quota full**: OneDrive Personal ima 5GB free, M365 daje 1TB. Backup je ~37MB pa nije problem.
