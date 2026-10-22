#!/usr/bin/env bash
# Deploy / update the separate VIASafe v2 stack on the server (run ON the server).
#
#   cd ~/viasafe-v2/infra/deploy/server-v2 && ./deploy.sh          first install or update (TEST mode:
#                                                                    no simulator, no demo data)
#   ./deploy.sh --with-demo                                          install/update in DEMO mode: start the
#                                                                    unit simulator (profile "demo") + load the demo data
#   ./deploy.sh --purge-demo                                         stop the simulator and remove ALL demo/seeded
#                                                                    data (python -m app.demo.purge_demo --yes); real data kept
#   ./deploy.sh --seed-only                                          demo mode only: reload all the demo data (purge + seed)
#   ./deploy.sh --refresh                                            demo mode only: re-date reports just before a demo
#   ./deploy.sh --status | --logs [service] | --down                 status / logs / stop (data kept)
#   ./deploy.sh --help
#
# See ../../../docs/TEST-PHASE.md (moving an old ~/viasafe-v2/api install: docs/MIGRATION-LAYOUT.md).
#
# Never touches the existing production stack: own project (viasafe-v2), own
# network and volumes, and it refuses to start if one of its ports is taken.
set -euo pipefail
cd "$(dirname "$0")"
P="viasafe-v2"
DC=(docker compose -p "$P" -f docker-compose.v2.yml --env-file .env)

WITH_DEMO=0
case "${1:-}" in
  --help|-h) sed -n '2,19p' "$0"; exit 0 ;;
  --with-demo) WITH_DEMO=1 ;;
  --purge-demo)
    echo "▶ stopping the demo unit simulator …"
    "${DC[@]}" --profile demo rm -sf simulator >/dev/null 2>&1 || true
    echo "▶ removing demo / seeded data (tagged rows only) …"
    "${DC[@]}" exec -T api python -m app.demo.purge_demo --yes --revoke-simulator; exit 0 ;;
  --refresh)   # just before a demo: re-date reports / parked vehicles (no rebuild, no purge)
    set -a; . ./.env; set +a
    "${DC[@]}" exec -T -e API_KEY="$SIM_API_KEY" api python -m app.demo.seed_presentation --refresh | tail -12; exit 0 ;;
  --status) "${DC[@]}" --profile demo ps; exit 0 ;;
  --logs) "${DC[@]}" --profile demo logs --tail 100 -f ${2:-}; exit 0 ;;
  --down) "${DC[@]}" --profile demo down; echo "stopped (volumes kept)"; exit 0 ;;
esac

rand() { openssl rand -hex "${1:-16}"; }

# ── .env (secrets generated once, chmod 600) ───────────────────────────────
# An install from before the folder restructure keeps its .env (with the DB /
# MinIO passwords the existing volumes were created with) in the old place:
# never generate new passwords over it — move it first (docs/MIGRATION-LAYOUT.md).
OLD_KIT="$HOME/viasafe-v2/api/deploy/server-v2"
if [ ! -f .env ] && [ -f "$OLD_KIT/.env" ]; then
  echo "✖ found the previous install's settings in $OLD_KIT/.env"
  echo "  move them here first (keeps the passwords of the existing data volumes):"
  echo "    mv $OLD_KIT/.env ./.env && chmod 600 .env"
  echo "  and the SFTP passwords: mv ~/viasafe-v2/api/secrets/sftp.env ~/viasafe-v2/infra/secrets/"
  echo "  — see ~/viasafe-v2/docs/MIGRATION-LAYOUT.md"
  exit 1
fi
if [ ! -f .env ]; then
  host_ip="$(hostname -I | awk '{print $1}')"
  cat > .env <<EOF
# VIASafe v2 server stack — generated $(date -Is). Keep private (chmod 600).
PUBLIC_HOST=${host_ip}
API_PORT=18080
WEB_PORT=13000
MEDIA_PORT=19200
WORKER_CONCURRENCY=2
SIM_MODE=presentation
ADMIN_EMAIL=admin@viasafe.local
ADMIN_PASSWORD=Vs-$(rand 6)
TESTER_PASSWORD=Ts-$(rand 6)
DB_PASSWORD=$(rand 16)
MINIO_USER=viasafe$(rand 3)
MINIO_PASSWORD=$(rand 16)
SECRET_KEY=$(rand 32)
SIM_API_KEY=vsk_$(rand 24)
EOF
  chmod 600 .env
  echo "▶ .env created (PUBLIC_HOST=${host_ip}). Check PUBLIC_HOST and the ports (nano .env), then run ./deploy.sh again."
  exit 0
fi
set -a; . ./.env; set +a
LS_PORT="${LS_PORT:-18085}"

# ── Label Studio settings (box review of the training data), generated once ─
LS_ENV=../../secrets/labelstudio.env
if [ ! -f "$LS_ENV" ]; then
  mkdir -p ../../secrets
  ( umask 077; cat > "$LS_ENV" <<EOF
# Label Studio of the web console's "Training data" page — generated $(date -Is). Keep private.
# Read by the labelstudio container (env_file) and by the API (/app/secrets/labelstudio.env).
LABEL_STUDIO_USERNAME=${ADMIN_EMAIL:-admin@viasafe.local}
LABEL_STUDIO_PASSWORD=Ls-$(rand 8)
LABEL_STUDIO_USER_TOKEN=$(rand 20)
LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN=true
LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK=true
VIASAFE_LS_URL=http://labelstudio:8080
VIASAFE_LS_PUBLIC_URL=http://${PUBLIC_HOST}:${LS_PORT}
VIASAFE_PUBLIC_API_URL=http://${PUBLIC_HOST}:${API_PORT}
EOF
  )
  echo "▶ Label Studio settings created: $(cd ../../secrets && pwd)/labelstudio.env"
fi

# ── safety checks ──────────────────────────────────────────────────────────
running_here="$(docker ps --filter "label=com.docker.compose.project=$P" -q | wc -l)"
for port in "$API_PORT" "$WEB_PORT" "$MEDIA_PORT" "$LS_PORT"; do
  if ss -ltn | awk '{print $4}' | grep -Eq "[:.]${port}$"; then
    if [ "$running_here" -gt 0 ] && docker ps --filter "label=com.docker.compose.project=$P" --format '{{.Ports}}' | grep -q ":${port}->"; then
      continue   # it is our own stack (update)
    fi
    echo "✖ port ${port} is already in use on this server — choose another one in .env"; exit 1
  fi
done
echo "▶ ports ${API_PORT} (API), ${WEB_PORT} (web), ${MEDIA_PORT} (media), ${LS_PORT} (Label Studio) are free or ours"

[ "${1:-}" = "--seed-only" ] && WITH_DEMO=1
if [ "${1:-}" != "--seed-only" ]; then
  echo "▶ building images (first time: 10–20 min) …"
  nice -n 10 "${DC[@]}" build
  echo "▶ starting the stack …"
  if [ "$WITH_DEMO" = 1 ]; then
    "${DC[@]}" --profile demo up -d
  else
    "${DC[@]}" up -d
    # test mode: a simulator left over from demo mode is stopped
    "${DC[@]}" --profile demo rm -sf simulator >/dev/null 2>&1 || true
  fi
fi

# ── wait for the API and the Cameroon OSRM ─────────────────────────────────
echo -n "▶ waiting for the API "
for _ in $(seq 1 120); do curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null && break; echo -n "."; sleep 5; done
curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null || { echo; echo "✖ API not answering — ./deploy.sh --logs api"; exit 1; }
echo " ok"
echo -n "▶ waiting for OSRM (first boot downloads + compiles Cameroon: 5–20 min) "
osrm_ok() { "${DC[@]}" exec -T api python -c "import httpx,sys; sys.exit(0 if httpx.get('http://osrm:5000/route/v1/driving/9.70,4.05;9.71,4.06',timeout=5).status_code==200 else 1)" 2>/dev/null; }
for _ in $(seq 1 240); do
  osrm_ok && break
  echo -n "."; sleep 10
done
if ! osrm_ok; then
  echo; echo "✖ OSRM is not answering — see: docker logs --tail 40 viasafe-v2-osrm-1"; exit 1
fi
echo " ok"

# ── demo mode only: simulator API key + demo data ──────────────────────────
if [ "$WITH_DEMO" = 1 ]; then
  "${DC[@]}" exec -T -e API_CLIENT_NAME=simulator -e API_CLIENT_KEY="$SIM_API_KEY" api python -m app.demo.ensure_api_client
  "${DC[@]}" --profile demo up -d simulator >/dev/null
  "${DC[@]}" --profile demo restart simulator >/dev/null
  echo "▶ loading demo data (presentation route, risk zones, reports, then the web test data) …"
  "${DC[@]}" exec -T -e API_KEY="$SIM_API_KEY" api python -m app.demo.seed_presentation | tail -8
  printf '%s' "$TESTER_PASSWORD" | "${DC[@]}" exec -T api python -m app.demo.seed_web_test --tester-password-stdin | tail -5
  MODE="DEMO (simulator on, demo data loaded)"
else
  MODE="TEST (no simulator, no demo data — ./deploy.sh --purge-demo removes old demo data)"
fi

cat <<EOF

✔ VIASafe v2 is running (production untouched) — mode: ${MODE}
  Web console : http://${PUBLIC_HOST}:${WEB_PORT}
  API         : http://${PUBLIC_HOST}:${API_PORT}   (docs: /docs)
  Admin       : ${ADMIN_EMAIL}    password: grep ADMIN_PASSWORD .env
  Label Studio: http://${PUBLIC_HOST}:${LS_PORT}   login: grep LABEL_STUDIO_ ../../secrets/labelstudio.env  (Training data page → Send to Label Studio)
  Status/logs : ./deploy.sh --status | --logs [api|web|worker|beat]
  Help        : ./deploy.sh --help
EOF
if [ "$WITH_DEMO" = 1 ]; then
  echo "  Tester      : testeur@viasafe.local   password: grep TESTER_PASSWORD .env"
  echo "  Before a demo (fresh reports): ./deploy.sh --refresh"
fi
