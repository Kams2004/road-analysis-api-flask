"""
CPU cost of the detectors of a job (Task: several models on the same video).

Loads the models exactly like the worker (pipeline._load_models: weights from
the model registry slots + crop classifiers + model cards' imgsz), then runs
them on the frames a job would sample from a clip (frame_skip 5) and reports
ms per sampled frame per model, OSD included, plus the wall time of the clip
with all models vs each model alone.

    tools/parked_eval/run_docker.sh bench_models.py --video "/videos/TEMCM127_….mp4" --start 2680 --dur 60
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, "/app")

import cv2  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--start", type=float, default=0)
    ap.add_argument("--dur", type=float, default=60)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()
    import torch
    torch.set_num_threads(args.threads)
    from app.core.config import settings
    from app.modules.m07_ingestion import pipeline as P
    from app.modules.m05_ai_detection import osd_parser as op
    keys = ["pothole", "signs", "speedbump", "vehicles"]
    P._load_models(keys)
    print("loaded:", {k: P._model_versions.get(k) for k in P._models}, flush=True)
    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 10
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(args.start * fps))
    frames, n = [], int(args.dur * fps)
    for i in range(n):
        ok, f = cap.read()
        if not ok:
            break
        if i % args.step == 0:
            frames.append(f)
    cap.release()
    print(f"{len(frames)} sampled frames ({args.dur:.0f} s @ {fps:.1f} fps, step {args.step}) "
          f"{frames[0].shape[1]}x{frames[0].shape[0]}", flush=True)
    for k in P._models:                      # warm-up (first call builds the graph)
        P._models[k](frames[0], verbose=False, **P._predict_kwargs(k))
    cost = {k: 0.0 for k in P._models}
    reader = op.OsdReader()
    t_osd = 0.0
    for idx, f in enumerate(frames):
        a = time.perf_counter()
        reader.read(f, idx * args.step / fps)
        t_osd += time.perf_counter() - a
        for k, m in P._models.items():
            a = time.perf_counter()
            if k == "vehicles":
                m(f, conf=0.35, iou=settings.IOU_THRESHOLD, classes=[2, 5, 7], verbose=False, **P._predict_kwargs(k))
            else:
                m(f, conf=settings.CONF_THRESHOLD, iou=settings.IOU_THRESHOLD, verbose=False, **P._predict_kwargs(k))
            cost[k] += time.perf_counter() - a
    nf = len(frames)
    print("ms per sampled frame:", {k: round(1000 * v / nf, 1) for k, v in cost.items()},
          "osd:", round(1000 * t_osd / nf, 1), flush=True)
    total_all = sum(cost.values()) + t_osd
    print(f"clip of {args.dur:.0f} s — all models + OSD: {total_all:.1f} s; "
          + "; ".join(f"{k} alone + OSD: {cost[k] + t_osd:.1f} s" for k in cost), flush=True)


if __name__ == "__main__":
    main()
