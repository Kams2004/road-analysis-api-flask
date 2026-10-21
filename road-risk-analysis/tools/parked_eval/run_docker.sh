#!/bin/sh
# Run a parked_eval script in the API image (CPU / memory capped).
#   tools/parked_eval/run_docker.sh extract.py [--only SEG]
#   tools/parked_eval/run_docker.sh evaluate.py [--tag after]
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
script=$1; shift
exec docker run --rm --cpus 4 -m 4g --network roaddemo_default \
  -v "$ROOT/backend/app":/app/app:ro -v "$ROOT/ai/models":/models:ro \
  -v "$ROOT/tools/parked_eval":/eval -v "$HOME/Videos/video":/videos:ro \
  -v "$HOME/Downloads":/downloads:ro -v "$ROOT/tools/parked_eval/.cache/paddleocr":/root/.paddleocr \
  -e PYTHONDONTWRITEBYTECODE=1 \
  road-analysis-api_api:latest python "/eval/$script" "$@"
