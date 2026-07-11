#!/bin/bash
set -e
cd ~/road-analysis-api

git pull origin main >> /tmp/redeploy.log 2>&1

# Rebuild and restart only the containerised services (db & minio are native)
docker compose build api worker >> /tmp/redeploy.log 2>&1
docker compose up -d --no-deps api worker redis >> /tmp/redeploy.log 2>&1

echo "DONE at $(date)" >> /tmp/redeploy.log
