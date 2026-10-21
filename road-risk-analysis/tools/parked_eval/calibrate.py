"""
Camera calibration of the parked-vehicle range model, from the cached
detections (stage A) and the labels — per segment, then per camera group.

1. Horizon and camera height (no labels needed). For a grounded vehicle of
   height H_v at range r: y_bottom − horizon = H_cam·f / r and box height
   h = H_v·f / r, so  y_bottom = horizon + (H_cam / H_v) · h.  A robust
   (trimmed) linear fit of y_bottom on h over the confident, uncut car boxes
   gives the horizon (intercept) and H_cam = slope × class height of a car.
2. Range scale K = H_cam·f (labels: `parked` vehicles). For a static vehicle
   1 / (y_bottom − horizon) = (r0 − D) / K, D = our travelled distance (OSD
   speed / GPS), so the slope of 1/(y_b − horizon) against D is −1/K.
   f = K / H_cam → vertical_fov_deg = 2·atan(0.5 / f).

    python tools/parked_eval/calibrate.py [--only SEG ...]
Prints one JSON camera override per segment and per video (to paste in
settings `camera_profiles` / the per-layout defaults).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAR_H = 1.5


def trimmed_line(xs, ys, keep=0.7, rounds=4):
    """Least squares y = a + b·x, dropping the worst residuals each round."""
    pts = list(zip(xs, ys))
    a = b = None
    for _ in range(rounds):
        n = len(pts)
        if n < 5:
            return None
        mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        if sxx <= 1e-12:
            return None
        b = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
        a = my - b * mx
        pts.sort(key=lambda p: abs(p[1] - a - b * p[0]))
        pts = pts[:max(5, int(len(pts) * keep))]
    return a, b, len(pts)


def horizon_points(data: dict) -> tuple[list, list]:
    W, H = data["width"], data["height"]
    xs, ys = [], []
    for fr in data["frames"]:
        for x1, y1, x2, y2, conf, cls in fr["boxes"]:
            if cls != "car" or conf < 0.5:
                continue
            yb, h = y2 / H, (y2 - y1) / H
            if yb > 0.97 or h < 0.03 or x1 <= 1 or x2 >= W - 2:      # cut by the frame
                continue
            xs.append(h)
            ys.append(yb)
    return xs, ys


def horizon_fit(data: dict):
    return trimmed_line(*horizon_points(data))


def ego_distance_fn(data: dict):
    """D(t): travelled metres from the OSD speeds (trapezoids), else the GPS fixes."""
    pts = [(fr["t"], fr["osd"]["speed_kmh"]) for fr in data["frames"] if fr["osd"] and fr["osd"]["speed_kmh"] is not None]
    if len(pts) < 2:
        return None
    cum = [(pts[0][0], 0.0)]
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        cum.append((tb, cum[-1][1] + (tb - ta) * (va + vb) / 2 / 3.6))

    def D(t):
        if t <= cum[0][0]:
            return cum[0][1]
        for (ta, da), (tb, db) in zip(cum, cum[1:]):
            if ta <= t <= tb:
                return da + (db - da) * (t - ta) / (tb - ta) if tb > ta else da
        return cum[-1][1]
    return D


def scale_fit(data: dict, labels: list, horizon: float):
    """K = H_cam·f from the labelled parked vehicles (median over vehicles)."""
    D = ego_distance_fn(data)
    if D is None:
        return []
    H = data["height"]
    fps = data["fps"]
    out = []
    for v in labels:
        if v["label"] != "parked":
            continue
        xs, ys = [], []
        for fr, x1, y1, x2, y2 in v["sightings"]:
            yb = y2 / H
            if yb > 0.97 or yb - horizon < 0.02:
                continue
            xs.append(D((fr - 1) / fps))
            ys.append(1.0 / (yb - horizon))
        if len(xs) < 5 or max(xs) - min(xs) < 15:
            continue
        fit = trimmed_line(xs, ys, keep=0.8)
        if fit and fit[1] < 0:
            out.append({"K": -1.0 / fit[1], "n": fit[2], "note": v.get("note", "")})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    per_video = defaultdict(lambda: {"horizon": [], "slope": [], "K": [], "pts": ([], []), "data": []})
    for cache in sorted((HERE / "cache").glob("*.json")):
        seg = cache.stem
        if args.only and seg not in args.only:
            continue
        data = json.loads(cache.read_text())
        hz = horizon_fit(data)
        vid = data["segment"]["video"]
        xs, ys = horizon_points(data)
        per_video[vid]["pts"][0].extend(xs)
        per_video[vid]["pts"][1].extend(ys)
        per_video[vid]["data"].append((data, seg))
        lf = HERE / "labels" / f"{seg}.json"
        labels = json.loads(lf.read_text())["vehicles"] if lf.exists() else []
        row = {"segment": seg, "size": f"{data['width']}x{data['height']}"}
        if hz:
            a, b, n = hz
            row.update(horizon_y=round(a, 3), cam_height_m=round(b * CAR_H, 2), n_boxes=n)
            ks = scale_fit(data, labels, a)
            row["K"] = [round(k["K"], 2) for k in ks]
            vid = data["segment"]["video"]
            per_video[vid]["horizon"].append(a)
            per_video[vid]["slope"].append(b)
            per_video[vid]["K"] += [k["K"] for k in ks]
        print(json.dumps(row))
    print("\nper video:")
    for vid, d in per_video.items():
        fit = trimmed_line(*d["pts"])
        if fit is None:
            continue
        hz, slope, n = fit
        hcam = slope * CAR_H
        out = {"video": vid, "horizon_y": round(hz, 3), "camera_height_m": round(hcam, 2), "n_boxes": n}
        ks = []
        for data, seg in d["data"]:           # range scale with the POOLED horizon
            lf = HERE / "labels" / f"{seg}.json"
            if lf.exists():
                ks += [k["K"] for k in scale_fit(data, json.loads(lf.read_text())["vehicles"], hz)]
        if ks:
            K = statistics.median(ks)
            out["K_all"] = [round(k, 2) for k in ks]
            f = K / hcam
            out.update(K=round(K, 2), n_parked=len(ks), vertical_fov_deg=round(math.degrees(2 * math.atan(0.5 / f)), 1))
        print(json.dumps(out))


if __name__ == "__main__":
    main()
