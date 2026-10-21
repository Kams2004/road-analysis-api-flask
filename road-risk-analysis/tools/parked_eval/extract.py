"""
Parked-vehicle evaluation — stage A (heavy, run in the API image).

For every segment of segments.json: read the frames the job worker would
sample (frame_index % frame_skip == 0, absolute frame numbers like the
pipeline), run the vehicles detector (model_vehicles.pt) with a LOW
confidence floor and ALL COCO vehicle classes (so stage B can replay any
detector_confidence / vehicle_classes setting) and the OSD reader, and cache
the raw boxes + OSD per sampled frame to cache/<segment>.json.

    docker run --rm --cpus 4 -m 4g \
      -v $PWD/backend/app:/app/app:ro -v $PWD/ai/models:/models:ro \
      -v $PWD/tools/parked_eval:/eval -v "$HOME/Videos/video":/videos:ro \
      -v "$HOME/Downloads":/downloads:ro \
      road-analysis-api_api:latest python /eval/extract.py [--only SEG ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

import cv2  # noqa: E402

HERE = Path(__file__).resolve().parent
RAW_CONF = 0.05
VEHICLE_CLASSES = ("car", "motorcycle", "bus", "truck")


def make_osd_reader():
    """The job's OSD reader (layout auto-detected); falls back to the stateless API."""
    from app.modules.m05_ai_detection import osd_parser as op
    try:
        reader = op.OsdReader()
        return lambda frame, t: reader.read(frame, t), reader
    except Exception as exc:                       # module being changed: stateless reader
        print(f"OsdReader unavailable ({exc}); using extract_osd", file=sys.stderr)
        voter = op.GpsVoter()
        return lambda frame, t: op.extract_osd(frame, voter), None


def osd_dict(o) -> dict:
    return {"lat": o.latitude, "lon": o.longitude, "speed_kmh": o.speed_kmh,
            "interpolated": bool(o.gps_interpolated), "vehicle_id": o.vehicle_id,
            "timestamp": o.timestamp.isoformat() if o.timestamp else None,
            "layout": getattr(o, "layout", None)}


def run_segment(model, class_ids, seg: dict, path: str, step: int, out: Path, no_osd: bool,
                osd_every: float) -> None:
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
    w, h = int(cap.get(3)), int(cap.get(4))
    first = int(seg["start_s"] * fps)
    last = first + int(seg["dur_s"] * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, first)
    read_osd, reader = (None, None) if no_osd else make_osd_reader()
    frames, t_det, t_osd = [], 0.0, 0.0
    idx, last_osd, last_osd_t = first, None, -1e9
    while idx < last:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1                     # absolute 1-based frame number (pipeline frame_num)
        if (idx - 1) % step:
            continue
        t = round((idx - 1) / fps, 3)
        a = time.perf_counter()
        res = model(frame, conf=RAW_CONF, iou=0.4, classes=class_ids, verbose=False, imgsz=640)
        b = time.perf_counter()
        if read_osd and t - last_osd_t >= osd_every - 1e-6:
            last_osd, last_osd_t = osd_dict(read_osd(frame, t)), t
        osd = last_osd
        c = time.perf_counter()
        t_det += b - a
        t_osd += c - b
        names = res[0].names
        boxes = [[*map(int, bx.xyxy[0].tolist()), round(bx.conf[0].item(), 4), str(names[int(bx.cls[0].item())])]
                 for bx in res[0].boxes]
        frames.append({"frame": idx, "t": t, "boxes": boxes, "osd": osd})
    cap.release()
    n = max(1, len(frames))
    data = {"segment": seg, "path": path, "fps": fps, "width": w, "height": h, "step": step,
            "raw_conf": RAW_CONF, "osd_layout": getattr(reader, "layout", None),
            "osd_every_s": osd_every,
            "timing_ms": {"detector": round(1000 * t_det / n, 1), "osd_per_sampled_frame": round(1000 * t_osd / n, 1)},
            "frames": frames}
    out.write_text(json.dumps(data))
    gps = sum(1 for f in frames if f["osd"] and f["osd"]["lat"] is not None)
    spd = sum(1 for f in frames if f["osd"] and f["osd"]["speed_kmh"] is not None)
    print(f"{seg['id']}: {len(frames)} frames {w}x{h}@{fps:.1f} layout={data['osd_layout']} gps={gps} "
          f"speed={spd} timing={data['timing_ms']}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", default=str(HERE / "segments.json"))
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--step", type=int, default=5, help="frame_skip of the job (processing.frame_skip_default)")
    ap.add_argument("--model", default="/models/model_vehicles.pt")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-osd", action="store_true")
    ap.add_argument("--osd-every", type=float, default=1.0,
                    help="OCR the OSD at most every N s of video (the overlay GPS / speed change at 1 Hz); "
                         "the sampled frames in between reuse the last read")
    args = ap.parse_args()
    from ultralytics import YOLO
    import torch
    torch.set_num_threads(4)
    model = YOLO(args.model)
    names = model.names
    class_ids = [i for i, n in names.items() if n in VEHICLE_CLASSES]
    spec = json.loads(Path(args.segments).read_text())
    cache = HERE / "cache"
    cache.mkdir(exist_ok=True)
    for seg in spec["segments"]:
        if args.only and seg["id"] not in args.only:
            continue
        out = cache / f"{seg['id']}.json"
        if out.exists() and not args.force:
            print(f"{seg['id']}: cached")
            continue
        run_segment(model, class_ids, seg, spec["videos"][seg["video"]], args.step, out, args.no_osd, args.osd_every)


if __name__ == "__main__":
    main()
