#!/usr/bin/env bash
# Copy the project to the server (run on YOUR PC). Only ~/viasafe-v2 is written on
# the server; the existing road-analysis-api-flask / road-analysis-frontend are not touched.
#   ./push.sh                       (default target ulrich@185.182.184.188)
#   SERVER=user@host ./push.sh
#   ./push.sh --minio               ship the MinIO image (not on Docker Hub any more)
#
# Server layout (same as road-risk-analysis/ on this PC, minus mobile/ and data):
#   ~/viasafe-v2/{backend,web,infra,ai/models,docs}
# Never copied: secrets (infra/secrets/*.env, keys), .env files, OSRM data, wheels,
# logs, node_modules / .next, __pycache__.
set -euo pipefail
cd "$(dirname "$0")/../../.."       # road-risk-analysis/
SERVER="${SERVER:-ulrich@185.182.184.188}"
if [ "${1:-}" = "--minio" ]; then
  # MinIO images are no longer on Docker Hub: send the one this PC already has.
  docker image inspect minio/minio:latest >/dev/null
  docker save minio/minio:latest | gzip | ssh "$SERVER" 'gunzip | docker load'
  echo "✔ MinIO image loaded on the server"
  exit 0
fi
ssh "$SERVER" 'mkdir -p ~/viasafe-v2/backend ~/viasafe-v2/web ~/viasafe-v2/infra ~/viasafe-v2/ai/models ~/viasafe-v2/docs'
RSYNC=(rsync -az --info=progress2
  --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/'
  --exclude '.env' --exclude '.env.*' --exclude '*.mp4')

echo "▶ backend/"
"${RSYNC[@]}" --exclude '.idea/' --exclude '.vscode/' \
  backend/ "$SERVER:~/viasafe-v2/backend/"
echo "▶ web/"
"${RSYNC[@]}" --exclude 'node_modules/' --exclude '.next/' --exclude 'tsconfig.tsbuildinfo' \
  web/ "$SERVER:~/viasafe-v2/web/"
echo "▶ infra/"
"${RSYNC[@]}" --exclude 'osrm/data/' --exclude 'wheels/' --exclude 'local/' \
  --exclude 'deploy/server-v2/.env' \
  --exclude 'secrets/*.env' --exclude 'secrets/*.key' --exclude 'secrets/*_ed25519' \
  --exclude 'secrets/*_rsa' --exclude 'secrets/*_ecdsa' \
  infra/ "$SERVER:~/viasafe-v2/infra/"
echo "▶ ai/models/"
"${RSYNC[@]}" --exclude 'incoming/' ai/models/ "$SERVER:~/viasafe-v2/ai/models/"   # incoming/ = downloaded packages
echo "▶ docs/"
"${RSYNC[@]}" docs/ "$SERVER:~/viasafe-v2/docs/"

echo "✔ copied. On the server:  cd ~/viasafe-v2/infra/deploy/server-v2 && ./deploy.sh"
echo "  SFTP passwords are NOT copied: create ~/viasafe-v2/infra/secrets/sftp.env on the server"
echo "  (cp sftp.env.example sftp.env && chmod 600 sftp.env) — see docs/SFTP-SOURCE.md"
echo "  First push after the folder restructure: see docs/MIGRATION-LAYOUT.md"
