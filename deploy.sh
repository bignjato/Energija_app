#!/bin/bash
cd /programi/hep_ha
echo "[$(date)] Pulling from GitHub..."
git pull origin main
echo "[$(date)] Rebuilding containers..."
docker-compose down && docker-compose up -d --build
echo "[$(date)] Deploy gotov!"
