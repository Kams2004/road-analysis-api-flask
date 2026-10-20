#!/usr/bin/env bash
# Copy the project to the server (run on YOUR PC). Only ~/viasafe-v2 is written on
# the server; the existing road-analysis-api-flask / road-analysis-frontend are not touched.
#   ./push.sh                       (default target ulrich@185.182.184.188)
#   SERVER=user@host ./push.sh
set -euo pipefail
cd "$(dirname "$0")/../.."          # api/
SERVER="${SERVER:-ulrich@185.182.184.188}"
if [ "${1:-}" = "--minio" ]; then
  # MinIO images are no longer on Docker Hub: send the one this PC already has.
  docker image inspect minio/minio:latest >/dev/null
  docker save minio/minio:latest | gzip | ssh "$SERVER" 'gunzip | docker load'
  echo "✔ MinIO image loaded on the server"
  exit 0
fi
ssh "$SERVER" 'mkdir -p ~/viasafe-v2/api'
rsync -az --info=progress2 \
  --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'road-risk-analysis-interface/node_modules/' --exclude 'road-risk-analysis-interface/.next/' \
  --exclude 'road-risk-analysis-interface/.env*' \
  --exclude 'viasafe-mobile/' --exclude 'osrm/data/' --exclude 'temp/' --exclude 'logs/' \
  --exclude 'wheels/' --exclude '*.mp4' --exclude '.env' \
  --exclude 'deploy/server-v2/.env' \
  --exclude 'secrets/*.env' --exclude 'secrets/*.key' --exclude 'secrets/*_ed25519' \
  --exclude 'secrets/*_rsa' --exclude 'secrets/*_ecdsa' \
  ./ "$SERVER:~/viasafe-v2/api/"
echo "✔ copied. On the server:  cd ~/viasafe-v2/api/deploy/server-v2 && ./deploy.sh"
echo "  SFTP passwords are NOT copied: create ~/viasafe-v2/api/secrets/sftp.env on the server"
echo "  (cp sftp.env.example sftp.env && chmod 600 sftp.env) — see docs/SFTP-SOURCE.md"
