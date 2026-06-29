#!/bin/bash
cd ~/road-analysis-api

git pull origin main >> /tmp/redeploy.log 2>&1
docker compose build api >> /tmp/redeploy.log 2>&1
docker compose up -d --no-deps api >> /tmp/redeploy.log 2>&1

echo "DONE at $(date)" >> /tmp/redeploy.log
