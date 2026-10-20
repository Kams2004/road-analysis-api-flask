#!/usr/bin/env bash
# Standalone pothole v1 test on a video (does NOT touch the platform).
#   ./run_test.sh                          whole LTTR video, every 2nd frame
#   ./run_test.sh --start 300 --seconds 120  only 2 minutes from 5:00
#   OUT=out-old ./run_test.sh --det /old/model_pothole.pt --road-ymin 0.45 --title "ANCIEN DETECTEUR"
#                                          old detector + road-area rule + new classifier
# Result: out/comparison.mp4 (left: detector, right: after classifier), out/detections.csv, out/summary.json
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"                       # road-risk-analysis/
VIDEO="${VIDEO:-$ROOT/data/videos/LTTR 458 AV_24_04 (2).mp4}"
OUT="${OUT:-out}"
mkdir -p "$OUT"
docker run --rm --name pothole-v1-test --cpus 4 -m 4g \
  -v "$VIDEO:/video.mp4:ro" \
  -v "$PWD/pothole_models_v1/install:/models:ro" \
  -v "$PWD/test_video.py:/test_video.py:ro" \
  -v "$PWD/$OUT:/out" \
  -v "$ROOT/ai/models/backup-before-v1:/old:ro" \
  -e YOLO_CONFIG_DIR=/tmp/yolo \
  road-analysis-api_api:latest python /test_video.py "$@"
ffmpeg -y -loglevel error -i "$OUT/comparison_raw.avi" -c:v libx264 -pix_fmt yuv420p -crf 23 "$OUT/comparison.mp4"
rm -f "$OUT/comparison_raw.avi"
echo "Video: $PWD/$OUT/comparison.mp4"
