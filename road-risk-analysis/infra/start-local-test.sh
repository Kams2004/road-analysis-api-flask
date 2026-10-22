#!/usr/bin/env bash
# Start the local VIASafe test environment after a reboot.
#
#   infra/start-local-test.sh            start everything (data is kept in Docker volumes)
#   infra/start-local-test.sh --seed    also load the web test data (purge + re-seed; demo data —
#                                    remove it again with: python -m app.demo.purge_demo --yes)
#   infra/start-local-test.sh --rebuild also rebuild the web console before starting it
#   infra/start-local-test.sh --demo    also start the demo unit simulator (off by default — test phase)
#
# Web console: http://localhost:3000
set -euo pipefail
INFRA="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$INFRA")"                       # road-risk-analysis/
cd "$ROOT"

API=roaddemo_api_1
WEB_DIR="$ROOT/web"
LOG_DIR="$ROOT/logs"
CREDS="$INFRA/local/web-test-credentials.txt"    # tester password (gitignored, chmod 600)
mkdir -p "$LOG_DIR"
SEED=0; REBUILD=0; DEMO=0
for arg in "$@"; do
  case "$arg" in
    --seed) SEED=1 ;;
    --rebuild) REBUILD=1 ;;
    --demo) DEMO=1 ;;
    *) echo "unknown option: $arg"; exit 1 ;;
  esac
done

echo "▶ Starting containers…"
docker start roaddemo_db_1 roaddemo_redis_1 roaddemo_osrm_1 roaddemo_minio_1 >/dev/null
docker start roaddemo_labelstudio_1 >/dev/null 2>&1 || true   # box review (Training data page), :8086
docker start "$API" >/dev/null
if [ "$DEMO" = 1 ]; then
  docker start roaddemo_simulator_1 >/dev/null 2>&1 || true
fi

echo "▶ Waiting for the API…"
for _ in $(seq 1 90); do
  curl -sf http://localhost:8080/health >/dev/null 2>&1 && break
  sleep 2
done
curl -sf http://localhost:8080/health >/dev/null || { echo "API not answering — see: docker logs $API"; exit 1; }

echo "▶ Worker + scheduler (inside the API container, one job at a time)…"
if ! docker top "$API" -eo pid,args | grep -q 'celery.*worker'; then
  docker exec -d "$API" sh -c "celery -A app.workers.celery_app worker -Q processing --concurrency=1 --loglevel=info > /tmp/worker.log 2>&1"
fi
if ! docker top "$API" -eo pid,args | grep -q 'celery.*beat'; then
  docker exec -d "$API" sh -c "celery -A app.workers.celery_app beat --loglevel=info -s /tmp/celerybeat-schedule > /tmp/beat.log 2>&1"
fi

if [ "$SEED" = 1 ]; then
  echo "▶ Resetting the web test data (a few minutes)…"
  if [ -f "$CREDS" ]; then
    docker exec -i -w /app -e PYTHONPATH=/app "$API" python -m app.demo.seed_web_test --tester-password-stdin < "$CREDS"
  else
    docker exec -w /app -e PYTHONPATH=/app "$API" python -m app.demo.seed_web_test
  fi
fi

echo "▶ Web console (production build)…"
if ss -ltn 2>/dev/null | grep -q ':3000 '; then
  echo "  already running on :3000"
else
  if [ "$REBUILD" = 1 ] || [ ! -f "$WEB_DIR/.next/BUILD_ID" ]; then
    (cd "$WEB_DIR" && NODE_OPTIONS=--max-old-space-size=3072 nice -n 10 npm run build) > "$LOG_DIR/web-build.log" 2>&1 \
      || { echo "  build failed — see $LOG_DIR/web-build.log"; exit 1; }
  fi
  (cd "$WEB_DIR" && setsid -f nohup npm run start -- -p 3000 > "$LOG_DIR/web-start.log" 2>&1 < /dev/null &)
  for _ in $(seq 1 30); do curl -s -o /dev/null http://localhost:3000/login && break; sleep 1; done
fi

echo
echo "✔ Ready: http://localhost:3000"
echo "  Sign in with your console account (bootstrap admin: BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD in infra/docker-compose.demo.yml)"
