"""
Standalone test (does NOT touch the platform): runs the pothole detector v1 and
its crop classifier on a video and writes a side-by-side comparison video.

  left  panel: every detector box with its confidence (what the detector alone sees)
  right panel: the same boxes after the classifier, coloured by decision
      green  : classifier agrees it is a pothole (P >= 0.5)        -> review, likely real
      orange : doubtful (0.0738 <= P < 0.5)                         -> review
      red    : rejected by the classifier (P < 0.0738, card threshold) -> auto-rejected
  (auto-accept is off in the card: 95 % precision was not reached)

Outputs in --out: comparison_raw.avi, detections.csv, summary.json.
Run through run_test.sh (Docker, platform image), not directly.
"""
import argparse, csv, json, os, time

import cv2
from ultralytics import YOLO

ap = argparse.ArgumentParser()
ap.add_argument("--video", default="/video.mp4")
ap.add_argument("--models", default="/models")
ap.add_argument("--out", default="/out")
ap.add_argument("--step", type=int, default=2, help="process every Nth frame")
ap.add_argument("--conf", type=float, default=0.25, help="detector display floor")
ap.add_argument("--start", type=float, default=0, help="start second")
ap.add_argument("--seconds", type=float, default=0, help="duration (0 = to the end)")
ap.add_argument("--det", default="", help="detector weights (default: <models>/model_pothole.pt)")
ap.add_argument("--det-imgsz", type=int, default=0, help="detector image size (default: its card, else 640)")
ap.add_argument("--road-ymin", type=float, default=0, help="drop boxes whose centre is above this fraction of the height (0 = off; platform rule 0.45)")
ap.add_argument("--title", default="DETECTEUR SEUL (v1)")
a = ap.parse_args()

det_path = a.det or f"{a.models}/model_pothole.pt"
try:
    det_card = json.load(open(det_path[:-3] + ".json"))
except OSError:                      # old model: no card
    det_card = {"thresholds": {"auto_reject": 0.0}, "imgsz": 640}
cls_card = json.load(open(f"{a.models}/model_pothole_classifier.json"))
DET_REJECT = det_card["thresholds"]["auto_reject"]
CLS_REJECT = cls_card["thresholds"]["auto_reject"]
PAD = cls_card.get("crop_pad_ratio", 0.15)
det = YOLO(det_path)
DET_IMGSZ = a.det_imgsz or det_card.get("imgsz", 640)
cls = YOLO(f"{a.models}/model_pothole_classifier.pt")
PI = list(cls.names.values()).index("pothole")

cap = cv2.VideoCapture(a.video)
fps, total = cap.get(cv2.CAP_PROP_FPS) or 25, int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
first = int(a.start * fps)
last = total if not a.seconds else min(total, first + int(a.seconds * fps))
BAR = 36
writer = cv2.VideoWriter(f"{a.out}/comparison_raw.avi", cv2.VideoWriter_fourcc(*"MJPG"),
                         fps / a.step, (W * 2, H + BAR))
GREEN, ORANGE, RED, WHITE = (0, 200, 0), (0, 165, 255), (0, 0, 255), (255, 255, 255)
GREY = (160, 160, 160)
counts = {"frames": 0, "det_boxes": 0, "off_road": 0, "below_det_reject": 0, "cls_likely": 0, "cls_doubtful": 0, "cls_rejected": 0}
rows, t0 = [], time.time()


def label(img, text, x, y, col):
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)


cap.set(cv2.CAP_PROP_POS_FRAMES, first)
fi = first
while fi < last:
    ok, frame = cap.read()
    if not ok:
        break
    if (fi - first) % a.step:
        fi += 1
        continue
    counts["frames"] += 1
    left, right = frame.copy(), frame.copy()
    for b in det.predict(frame, imgsz=DET_IMGSZ, conf=a.conf, verbose=False)[0].boxes:
        conf = float(b.conf)
        x1, y1, x2, y2 = map(int, b.xyxy[0])
        counts["det_boxes"] += 1
        cv2.rectangle(left, (x1, y1), (x2, y2), WHITE, 2)
        label(left, f"{conf:.2f}", x1, max(12, y1 - 4), WHITE)
        if a.road_ymin and (y1 + y2) / 2 < a.road_ymin * H:
            counts["off_road"] += 1
            cv2.rectangle(right, (x1, y1), (x2, y2), GREY, 1)
            label(right, "hors route", x1, max(12, y1 - 4), GREY)
            continue
        if conf < DET_REJECT:
            counts["below_det_reject"] += 1
            continue
        pw, ph = int((x2 - x1) * PAD), int((y2 - y1) * PAD)
        crop = frame[max(0, y1 - ph):min(H, y2 + ph), max(0, x1 - pw):min(W, x2 + pw)]
        p = float(cls.predict(crop, imgsz=cls_card.get("imgsz", 224), verbose=False)[0].probs.data[PI]) if crop.size else 0.0
        if p < CLS_REJECT:
            col, dec = RED, "rejected"
        elif p < 0.5:
            col, dec = ORANGE, "doubtful"
        else:
            col, dec = GREEN, "likely"
        counts[f"cls_{dec}"] += 1
        cv2.rectangle(right, (x1, y1), (x2, y2), col, 2 if dec != "rejected" else 1)
        label(right, f"P={p:.2f}", x1, max(12, y1 - 4), col)
        rows.append({"frame": fi, "time_s": round(fi / fps, 1), "det_conf": round(conf, 3), "cls_p": round(p, 3),
                     "decision": dec, "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    canvas = cv2.copyMakeBorder(cv2.hconcat([left, right]), BAR, 0, 0, 0, cv2.BORDER_CONSTANT, value=(30, 30, 30))
    mm, ss = divmod(int(fi / fps), 60)
    label(canvas, f"{a.title}   t={mm:02d}:{ss:02d}", 10, 24, WHITE)
    label(canvas, "APRES CLASSIFIEUR: vert=probable  orange=douteux  rouge=rejete", W + 10, 24, WHITE)
    writer.write(canvas)
    if counts["frames"] % 200 == 0:
        el = time.time() - t0
        done = (fi - first) / max(1, last - first)
        print(f"{done:5.1%}  frame {fi}/{last}  {el / 60:.1f} min elapsed, ~{el / max(done, 1e-6) / 60 - el / 60:.0f} min left  {counts}", flush=True)
    fi += 1

writer.release()
with open(f"{a.out}/detections.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["frame", "time_s", "det_conf", "cls_p", "decision", "x1", "y1", "x2", "y2"])
    w.writeheader(); w.writerows(rows)
counts["minutes"] = round((time.time() - t0) / 60, 1)
counts["thresholds"] = {"det": det_path, "det_imgsz": DET_IMGSZ, "det_reject": DET_REJECT, "cls_reject": CLS_REJECT,
                        "display_conf": a.conf, "road_ymin": a.road_ymin, "step": a.step}
json.dump(counts, open(f"{a.out}/summary.json", "w"), indent=1)
print("DONE", counts, flush=True)
