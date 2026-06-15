# Kubernetes deployment

Manifesti za pokretanje HEP Energy Monitora na Kubernetesu. Arhitektura preslikava
docker-compose: **web** (gunicorn) i **sync** (`sync_loop.sh`) kao dva kontejnera u
**jednom podu** koji dijele isti `/data` (SQLite + WAL). `replicas: 1` + `Recreate`
strategija — SQLite ne podnosi više writer-podova, a `ReadWriteOnce` PVC ionako
dopušta samo jedan pod.

## Preduvjeti

- Kubernetes klaster s nekom `StorageClass` (postavi u `pvc.yaml` ako default ne postoji)
- NGINX Ingress Controller + cert-manager (za TLS) — ili prilagodi `ingress.yaml`
- Image objavljen u registryju dostupnom klasteru

## 1. Build & push image

```bash
docker build -t ghcr.io/bignjato/hep_ha:latest .
docker push ghcr.io/bignjato/hep_ha:latest
```

Promijeni `image:` u `deployment.yaml` (i tag u `kustomization.yaml`) na svoj registry.

## 2. Secret

```bash
cd k8s
cp secret.example.yaml secret.yaml
python3 -c "import secrets; print(secrets.token_hex(32))"   # SECRET_KEY
$EDITOR secret.yaml      # popuni HEP/SMA/HA kredencijale; secret.yaml NIJE u gitu
```

## 3. Host & TLS

U `ingress.yaml` postavi `host` i `secretName`, te `cluster-issuer` na svoj cert-manager issuer.

## 4. Deploy

```bash
kubectl apply -k k8s/                 # kustomize (preporučeno)
# ili pojedinačno:
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/pvc.yaml k8s/secret.yaml k8s/deployment.yaml k8s/service.yaml k8s/ingress.yaml
```

## Provjera / operacije

```bash
kubectl -n hep-energy get pods
kubectl -n hep-energy logs deploy/hep-energy -c web -f
kubectl -n hep-energy logs deploy/hep-energy -c sync -f
kubectl -n hep-energy exec -it deploy/hep-energy -c web -- sqlite3 /data/hep_energy.db

# Manualni scrape
kubectl -n hep-energy exec deploy/hep-energy -c sync -- python /app/hep_scraper.py --dani 7
```

## Napomene

- Prvi login `admin` / `INITIAL_ADMIN_PASSWORD` → setup wizard → promijeni lozinku.
- Backup baze radi `sync_loop.sh` u `/data/backups/` (na PVC-u). Za off-site postavi
  `OFFSITE_BACKUP_*` u Secretu (vidi `ONEDRIVE_SETUP.md`).
- `SECRET_KEY` rotacija invalidira sve sesije — postavi jednom i drži.
- Rate limit koristi in-memory storage; uz `-w 2` gunicorn worker-a limit je per-worker.
