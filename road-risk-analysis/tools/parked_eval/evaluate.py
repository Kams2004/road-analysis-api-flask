"""
Parked-vehicle evaluation — stage B (light; run in the API image so the
backend imports are the real ones).

Replays the cached detector boxes + OSD of stage A (extract.py) through
EXACTLY the job worker's code path:

    pipeline._JobContext.observe_osd   → ego path
    pipeline._vehicle_tracker          → static_check.Tracker (motion prediction)
    pipeline._observe_vehicles         → class / confidence / mask filter + tracking
    pipeline._judge_vehicle_track      → pv.prepare / OSRM snap / pv.finish (+ queue peers)

with the `parked_vehicle` settings (defaults, optionally patched with
--settings patch.json), then writes per segment:

    out/<tag>/tracks.json        every track: verdict, subtype, score, motion,
                                 every reason {code, value, threshold, passed},
                                 every feature, the sightings (frame + box)
    out/<tag>/sheets/<seg>.jpg   contact sheet: 5 sightings per track, box drawn
    out/<tag>/videos/<seg>.mp4   (--video) annotated sampled frames
    out/<tag>/report.json/.md    confusion matrix + precision / recall of
                                 `parked` against labels/<seg>.json

Labels (labels/<segment>.json): {"vehicles": [{"label": "parked" |
"moving_same_dir" | "oncoming" | "queue" | "crossing" | "ignore",
"sightings": [[frame, x1, y1, x2, y2], ...], "note": "..."}]}. A track takes
the label of the labelled vehicle whose sightings it overlaps (same frame,
IoU ≥ 0.4) the most; a labelled vehicle is "flagged" when any of its tracks
is stored as parked (that is what a validator sees).

    tools/parked_eval/run_docker.sh evaluate.py --tag before [--settings p.json] [--sheets] [--video]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
_BACKEND = HERE.parent.parent / "backend"
if "--backend" in sys.argv:            # e.g. an export of the previous commit, for the "before" numbers
    _BACKEND = Path(sys.argv[sys.argv.index("--backend") + 1]).resolve()
sys.path.insert(0, str(_BACKEND) if (_BACKEND / "app").is_dir() else "/app")
try:                                   # light run outside the API image (test venv): the
    import ultralytics  # noqa: F401   # detector is never called here, only imported by pipeline
except ImportError:
    import types
    sys.modules["ultralytics"] = types.SimpleNamespace(YOLO=None)  # type: ignore[assignment]

import cv2  # noqa: E402
import numpy as np  # noqa: E402

# video paths of the cache are the container's; map them when run on the host
_HOST_PATHS = {"/videos/": str(Path.home() / "Videos/video") + "/", "/downloads/": str(Path.home() / "Downloads") + "/"}


def host_path(p: str) -> str:
    if Path(p).exists():
        return p
    for a, b in _HOST_PATHS.items():
        if p.startswith(a):
            return b + p[len(a):]
    return p
TRUTH = ("parked", "moving_same_dir", "oncoming", "queue", "crossing", "ignore")
VERDICT_COLOURS = {"parked": (0, 0, 255), "moving": (0, 200, 0), "traffic_queue": (255, 160, 0),
                   "insufficient_evidence": (0, 200, 255), "too_far": (160, 160, 160)}


class _Frame:
    """Stand-in for the decoded frame: _observe_vehicles only needs its shape and copy()."""

    def __init__(self, h: int, w: int):
        self.shape = (h, w, 3)

    def copy(self):
        return self


class _Box:
    def __init__(self, b):
        self.xyxy = [_T(b[:4])]
        self.conf = [_T(b[4])]
        self.cls = [_T(b[5])]


class _T:
    def __init__(self, v):
        self.v = v

    def tolist(self):
        return list(self.v)

    def item(self):
        return self.v


def fake_results(boxes: list, conf_floor: float, classes: list[str]):
    """Detector output as the pipeline sees it: the model is called with
    conf=detector_confidence and classes=vehicle_classes."""
    names = {i: c for i, c in enumerate(("car", "motorcycle", "bus", "truck"))}
    idx = {c: i for i, c in names.items()}
    kept = [_Box([*b[:5], idx[b[5]]]) for b in boxes if b[4] >= conf_floor and b[5] in classes]
    return [SimpleNamespace(names=names, boxes=kept)]


def to_osd(d: dict | None):
    from app.modules.m05_ai_detection.osd_parser import OSDData
    if not d:
        return OSDData()
    ts = datetime.fromisoformat(d["timestamp"]) if d.get("timestamp") else None
    o = OSDData(latitude=d.get("lat"), longitude=d.get("lon"), speed_kmh=d.get("speed_kmh"),
                gps_interpolated=bool(d.get("interpolated")), vehicle_id=d.get("vehicle_id"), timestamp=ts)
    if hasattr(o, "layout"):
        o.layout = d.get("layout")
    return o


def load_settings(patch_path: str | None):
    from app.modules.m05_ai_detection.parked_vehicle import ParkedVehicleSettings
    base = ParkedVehicleSettings().model_dump()
    if patch_path:
        base = ParkedVehicleSettings.merge_patch(base, json.loads(Path(patch_path).read_text()))
    return ParkedVehicleSettings(**base)


def strip_ego(data: dict, layouts: list[str]) -> None:
    """Drop the OSD GPS + speed of the frames read with these layouts — replays a
    job where the overlay could not be read (e.g. `besafe` before the reader supported it)."""
    for fr in data["frames"]:
        o = fr.get("osd")
        if o and (o.get("layout") in layouts or "all" in layouts):
            fr["osd"] = {**o, "lat": None, "lon": None, "speed_kmh": None}


async def replay(data: dict, s, osrm: bool) -> list[dict]:
    from app.modules.m07_ingestion import pipeline as P
    from app.modules.m05_ai_detection import parked_vehicle as pv
    fps = data["fps"]
    ctx = SimpleNamespace(pv=s, pv_version=pv.rules_version(s), ego=pv.EgoPath(), vehicle_peers=__import__(
        "collections").deque(maxlen=200), fps=fps, osd_times=[], osrm_down=None if osrm else "disabled (harness)",
        job=SimpleNamespace(id="parked-eval"))
    ctx.video_time = lambda n: P._JobContext.video_time(ctx, n)
    if hasattr(pv, "HorizonEstimator"):
        ctx.horizon = pv.HorizonEstimator(min_boxes=s.auto_calibrate_min_boxes)
    tracker = P._vehicle_tracker(s)
    stats = P._Stats()
    frame = _Frame(data["height"], data["width"])
    out: list[dict] = []
    step = data["step"]

    async def judge_track(tr, others):
        if hasattr(P, "_judge_vehicle_track"):
            return await P._judge_vehicle_track(ctx, tr, others)
        # older pipeline (before the function was split out of _finalize_vehicle): same steps
        sightings = P._vehicle_track_input(tr)
        if not sightings or tr.best is None:
            return None
        osd = tr.best.osd
        features, _cam = pv.prepare(sightings, ctx.ego.window(sightings[0].t_s, sightings[-1].t_s), s,
                                    osd.vehicle_id)
        distance, note = None, ""
        if pv.needs_road_check(features, s):
            distance, note = await P._road_distance(ctx, features.target_lat, features.target_lon)
        ev = pv.finish(features, s, road_distance_m=distance, road_note=note,
                       queue_peers=P._queue_peers(ctx, tr, features, others), vehicle_id=osd.vehicle_id)
        ctx.vehicle_peers.append(pv.peer_summary(features, s))
        return ev, sightings

    async def judge(done, others):
        for tr in done:
            r = await judge_track(tr, others)
            if r is None:
                continue
            ev, sightings = r
            out.append(track_record(ev, sightings, tr))

    deferred = hasattr(P, "_vehicle_batch")      # held back until the camera self-calibrates
    if deferred:
        ctx.vehicle_pending = []
    for fr in data["frames"]:
        osd = to_osd(fr["osd"])
        P._JobContext.observe_osd(ctx, fr["frame"], osd)
        res = fake_results(fr["boxes"], s.detector_confidence, s.vehicle_classes)
        P._observe_vehicles(ctx, tracker, res, frame, fr["frame"], osd, step, stats)
        batch = tracker.expire(fr["frame"], step)
        if deferred:
            batch = P._vehicle_batch(ctx, batch)
        await judge(batch, (*tracker.open, *batch))
    batch = tracker.flush()
    if deferred:
        batch = P._vehicle_batch(ctx, batch, final=True)
    await judge(batch, batch)
    return out


def track_record(ev, sightings, tr) -> dict:
    j = ev.judgement
    return {
        "verdict": ev.verdict, "subtype": ev.subtype, "score": ev.score, "store": ev.store,
        "motion": j.motion, "fallback": j.fallback, "blockers": [b[0] for b in j.blockers],
        "class": ev.vehicle_class, "reasons": {r.code: {"value": r.value, "threshold": r.threshold, "passed": r.passed}
                                               for r in j.reasons},
        "failed": j.failed(), "features": ev.features.as_dict(), "components": ev.components,
        "sightings": [[x.frame_num, *map(int, x.box), round(x.conf, 3)] for x in sightings],
        "best_frame": tr.best.frame_num,
    }


# ── labels / metrics ─────────────────────────────────────────────────────────

def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def match_labels(tracks: list[dict], labels: list[dict]) -> None:
    """track['truth'] / track['label_idx'] from the labelled vehicles' sightings."""
    by_frame = defaultdict(list)
    for li, v in enumerate(labels):
        for fr, *box in v["sightings"]:
            by_frame[fr].append((li, box))
    for t in tracks:
        votes = Counter()
        for fr, *rest in t["sightings"]:
            for li, box in by_frame.get(fr, ()):
                if iou(rest[:4], box) >= 0.4:
                    votes[li] += 1
        if votes:
            li, n = votes.most_common(1)[0]
            t["label_idx"], t["truth"], t["label_overlap"] = li, labels[li]["label"], n
        else:
            t["label_idx"], t["truth"] = None, None


def metrics(per_seg: dict[str, tuple[list[dict], list[dict]]]) -> dict:
    """Vehicle-level confusion: truth label × outcome. Outcome of a labelled
    vehicle = `parked` if any of its tracks is parked, else the verdict of its
    longest track, else `no_track`. Unlabelled tracks stored as parked are
    listed (they must be labelled to be counted)."""
    conf = defaultdict(Counter)
    unlabelled_parked = []
    for seg, (tracks, labels) in per_seg.items():
        for li, v in enumerate(labels):
            mine = [t for t in tracks if t.get("label_idx") == li]
            if not mine:
                outcome = "no_track"
            elif any(t["verdict"] == "parked" for t in mine):
                outcome = "parked"
            else:
                outcome = max(mine, key=lambda t: len(t["sightings"]))["verdict"]
            conf[v["label"]][outcome] += 1
        for t in tracks:
            if t["verdict"] == "parked" and t.get("label_idx") is None:
                unlabelled_parked.append({"segment": seg, "best_frame": t["best_frame"], "class": t["class"]})
    truth_parked = sum(conf["parked"].values())
    tp = conf["parked"]["parked"]
    fp = sum(c["parked"] for k, c in conf.items() if k not in ("parked", "ignore"))
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / truth_parked if truth_parked else None
    return {"confusion": {k: dict(v) for k, v in conf.items()}, "tp": tp, "fp": fp,
            "fn": truth_parked - tp, "precision": prec, "recall": rec,
            "unlabelled_parked": unlabelled_parked}


def report_md(m: dict, tag: str) -> str:
    cols = ["parked", "moving", "traffic_queue", "insufficient_evidence", "too_far", "not_grounded", "off_road",
            "no_track"]
    lines = [f"# parked_eval — {tag}", "",
             f"precision(parked) = {m['precision']}  recall(parked) = {m['recall']}  "
             f"tp={m['tp']} fp={m['fp']} fn={m['fn']}", "",
             "| truth \\ verdict | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for k in TRUTH:
        row = m["confusion"].get(k)
        if row:
            lines.append(f"| {k} | " + " | ".join(str(row.get(c, 0)) for c in cols) + " |")
    if m["unlabelled_parked"]:
        lines += ["", f"unlabelled tracks flagged parked: {len(m['unlabelled_parked'])}"]
        lines += [f"- {u}" for u in m["unlabelled_parked"]]
    return "\n".join(lines) + "\n"


# ── visual output ────────────────────────────────────────────────────────────

def read_frames(path: str, wanted: set[int]) -> dict[int, np.ndarray]:
    if not wanted:
        return {}
    cap = cv2.VideoCapture(path)
    lo, hi = min(wanted), max(wanted)
    cap.set(cv2.CAP_PROP_POS_FRAMES, lo - 1)
    out, n = {}, lo - 1
    while n < hi:
        ok, f = cap.read()
        if not ok:
            break
        n += 1
        if n in wanted:
            out[n] = f
    cap.release()
    return out


def short_reasons(t: dict) -> str:
    f, r = t["features"], t["reasons"]
    def g(code):
        x = r.get(code, {}).get("value")
        return f"{x:.1f}" if isinstance(x, float) else str(x)
    return (f"ego={f['ego_mode']} v={g('ego_speed')} tv={g('target_speed')}/{g_thr(r, 'target_speed')} "
            f"drift={g('world_drift')} gc={g('ground_contact')} cor={int(bool(f['in_corridor_at_closest']))} "
            f"{f['side_at_closest'][0]} n={f['sightings']} {f['duration_s']:.1f}s")


def g_thr(r, code):
    x = r.get(code, {}).get("threshold")
    return f"{x:.1f}" if isinstance(x, float) else str(x)


def contact_sheet(tracks: list[dict], frames: dict, out: Path, min_sightings: int, min_area: float = 0.0,
                  tile=180) -> None:
    rows = []
    for ti, t in enumerate(tracks):
        if len(t["sightings"]) < min_sightings or t["features"]["closest_area"] < min_area:
            continue
        sts = t["sightings"]
        pick = sorted({sts[int(k * (len(sts) - 1) / 4)][0] for k in range(5)})
        cells = []
        for fr in pick:
            s = next(x for x in sts if x[0] == fr)
            img = frames.get(fr)
            if img is None:
                cells.append(np.zeros((tile, int(tile * 1.6), 3), np.uint8))
                continue
            img = img.copy()
            col = VERDICT_COLOURS.get(t["verdict"], (255, 255, 255))
            cv2.rectangle(img, tuple(s[1:3]), tuple(s[3:5]), col, 2)
            cells.append(cv2.resize(img, (int(tile * 1.6), tile)))
        while len(cells) < 5:
            cells.append(np.zeros_like(cells[0]))
        row = np.hstack(cells)
        bar = np.zeros((36, row.shape[1], 3), np.uint8)
        lab = f" truth={t.get('truth')}" if t.get("truth") else ""
        cv2.putText(bar, f"#{ti} {t['verdict']} {t['subtype'] or ''} {t['class']} s={t['score']:.2f} "
                         f"mot={t['motion']} f{sts[0][0]}-{sts[-1][0]}{lab}", (4, 14), 0, 0.45, (255, 255, 255), 1)
        cv2.putText(bar, short_reasons(t), (4, 31), 0, 0.42, (0, 255, 255), 1)
        rows.append(np.vstack([bar, row]))
    if not rows:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    per = 8
    for k in range(0, len(rows), per):
        cv2.imwrite(str(out.with_name(f"{out.stem}_{k // per}.jpg")), np.vstack(rows[k:k + per]),
                    [cv2.IMWRITE_JPEG_QUALITY, 72])


def annotated_video(data: dict, tracks: list[dict], frames: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    by_frame = defaultdict(list)
    for ti, t in enumerate(tracks):
        for s in t["sightings"]:
            by_frame[s[0]].append((ti, t, s))
    vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), data["fps"] / data["step"],
                         (data["width"], data["height"]))
    for fr in data["frames"]:
        img = frames.get(fr["frame"])
        if img is None:
            continue
        img = img.copy()
        for ti, t, s in by_frame.get(fr["frame"], ()):
            col = VERDICT_COLOURS.get(t["verdict"], (255, 255, 255))
            cv2.rectangle(img, tuple(s[1:3]), tuple(s[3:5]), col, 2)
            cv2.putText(img, f"#{ti} {t['verdict'][:6]}", (s[1], max(10, s[2] - 4)), 0, 0.45, col, 1)
        o = fr["osd"] or {}
        cv2.putText(img, f"f{fr['frame']} v={o.get('speed_kmh')} gps={o.get('lat') is not None}",
                    (8, data["height"] - 10), 0, 0.5, (0, 255, 255), 1)
        vw.write(img)
    vw.release()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="current")
    ap.add_argument("--settings", help="JSON patch of the parked_vehicle settings")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--sheets", action="store_true", help="contact sheets of the tracks")
    ap.add_argument("--sheet-min-sightings", type=int, default=3)
    ap.add_argument("--sheet-min-area", type=float, default=0.0, help="closest box area / frame area")
    ap.add_argument("--video", action="store_true", help="annotated video of the sampled frames")
    ap.add_argument("--osrm", action="store_true", help="query OSRM for the road-distance rule")
    ap.add_argument("--backend", help="backend directory to import (default: this checkout)")
    ap.add_argument("--no-ego-layouts", nargs="*", help="drop OSD GPS + speed for these layouts ('all' = every frame)")
    args = ap.parse_args()
    s = load_settings(args.settings)
    out_dir = HERE / "out" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    per_seg, dump = {}, {}
    for cache in sorted((HERE / "cache").glob("*.json")):
        seg = cache.stem
        if args.only and seg not in args.only:
            continue
        data = json.loads(cache.read_text())
        if args.no_ego_layouts:
            strip_ego(data, args.no_ego_layouts)
        tracks = asyncio.run(replay(data, s, args.osrm))
        lf = HERE / "labels" / f"{seg}.json"
        labels = json.loads(lf.read_text())["vehicles"] if lf.exists() else []
        match_labels(tracks, labels)
        per_seg[seg] = (tracks, labels)
        dump[seg] = tracks
        verdicts = Counter(t["verdict"] for t in tracks)
        print(f"{seg}: {len(tracks)} tracks {dict(verdicts)} labels={len(labels)}", flush=True)
        if args.sheets or args.video:
            want = {x[0] for t in tracks for x in t["sightings"]} if args.video else \
                {x[0] for t in tracks if len(t["sightings"]) >= args.sheet_min_sightings for x in t["sightings"]}
            frames = read_frames(host_path(data["path"]), want)
            if args.sheets:
                contact_sheet(tracks, frames, out_dir / "sheets" / f"{seg}.jpg", args.sheet_min_sightings,
                              args.sheet_min_area)
            if args.video:
                annotated_video(data, tracks, frames, out_dir / "videos" / f"{seg}.mp4")
    tracks_file = out_dir / "tracks.json"
    if args.only and tracks_file.exists():      # partial run: keep the other segments' tracks
        dump = {**json.loads(tracks_file.read_text()), **dump}
    tracks_file.write_text(json.dumps(dump, indent=1, default=str))
    m = metrics(per_seg)
    (out_dir / "report.json").write_text(json.dumps(m, indent=1))
    (out_dir / "report.md").write_text(report_md(m, args.tag))
    print(report_md(m, args.tag))


if __name__ == "__main__":
    main()
