"""
Old vs new traffic-sign model on the same video frames (offline, nothing is stored in the platform).

    python compare.py VIDEO --old OLD.pt --det NEW_DET.pt --cls NEW_CLS.pt --out DIR [--every-s 1] [--mode assisted]

Runs from ai/training/traffic-signs (the new two-stage pipeline is its signs.inference.predict.TwoStage, i.e. the
same thresholds and crop rule as the API). For every sampled frame where either model finds something, writes
DIR/frames/t<seconds>.jpg — old model left, new model right — and DIR/summary.json + DIR/index.html.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path.cwd()))
from signs.inference.predict import TwoStage, draw  # noqa: E402


def old_predict(model, img: Image.Image, conf: float) -> list[dict]:
    r = model.predict(img, conf=conf, device="cpu", verbose=False)[0]
    out = []
    if r.boxes is None:
        return out
    for b, c, k in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(), r.boxes.cls.cpu().numpy().astype(int)):
        out.append({"xyxy": [round(float(v), 1) for v in b], "name": model.names[int(k)], "conf": round(float(c), 3)})
    return out


def draw_old(img: Image.Image, dets: list[dict]) -> Image.Image:
    img = img.copy()
    d = ImageDraw.Draw(img)
    for x in dets:
        x0, y0, x1, y1 = x["xyxy"]
        d.rectangle((x0, y0, x1, y1), outline=(220, 40, 40), width=2)
        label = f"{x['name']} {x['conf']:.2f}"
        d.rectangle((x0, max(0, y0 - 14), x0 + d.textlength(label) + 6, y0), fill=(220, 40, 40))
        d.text((x0 + 3, max(0, y0 - 13)), label, fill=(255, 255, 255))
    return img


def banner(img: Image.Image, text: str) -> Image.Image:
    d = ImageDraw.Draw(img)
    d.rectangle((0, img.height - 22, img.width, img.height), fill=(0, 0, 0))
    d.text((6, img.height - 18), text, fill=(255, 255, 255))
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--old", required=True)
    ap.add_argument("--det", required=True)
    ap.add_argument("--cls", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--every-s", type=float, default=1.0)
    ap.add_argument("--old-conf", type=float, default=0.25)
    ap.add_argument("--mode", default="assisted", choices=["assisted", "automatic"])
    a = ap.parse_args()

    from ultralytics import YOLO
    import torch
    torch.set_num_threads(4)

    old = YOLO(a.old)
    new = TwoStage(a.det, a.cls, None, a.mode)
    out = Path(a.out)
    (out / "frames").mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(a.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, round(fps * a.every_s))
    rows, old_names, new_codes, new_dec = [], Counter(), Counter(), Counter()
    frames_old = frames_new = 0
    for i in range(0, n, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, frame = cap.read()
        if not ok:
            break
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        o = old_predict(old, img, a.old_conf)
        nw = new(img)
        t = round(i / fps, 1)
        if i // step % 60 == 0:
            print(f"t={t:.0f}s / {n / fps:.0f}s", flush=True)
        if not o and not nw:
            continue
        frames_old += bool(o)
        frames_new += bool(nw)
        old_names.update(x["name"] for x in o)
        new_codes.update(f"{x['code'] or x['det_class']}" for x in nw if x["decision"] != "auto_reject")
        new_dec.update(x["decision"] for x in nw)
        left = banner(draw_old(img, o), f"OLD model  t={t}s  {len(o)} box(es)")
        right = banner(draw(img, nw), f"NEW v1 ({a.mode})  green=auto-accept orange=review grey=auto-reject")
        both = Image.new("RGB", (img.width * 2 + 8, img.height), (255, 255, 255))
        both.paste(left, (0, 0))
        both.paste(right, (img.width + 8, 0))
        name = f"t{int(t * 10):06d}.jpg"
        both.save(out / "frames" / name, quality=85)
        rows.append({"t": t, "file": f"frames/{name}", "old": o,
                     "new": [{k: x[k] for k in ("xyxy", "det_class", "det_conf", "code", "name_fr", "cls_p",
                                                 "decision", "reason")} for x in nw]})
    cap.release()

    summary = {"video": a.video, "sampled_every_s": a.every_s, "frames_with_old": frames_old,
               "frames_with_new": frames_new, "old_boxes": sum(old_names.values()), "old_by_name": dict(old_names),
               "new_boxes": sum(new_dec.values()), "new_by_decision": dict(new_dec),
               "new_kept_by_code": dict(new_codes.most_common())}
    (out / "summary.json").write_text(json.dumps({"summary": summary, "frames": rows}, indent=1, ensure_ascii=False))
    cards = "\n".join(
        f'<figure><a href="{r["file"]}"><img loading="lazy" src="{r["file"]}"></a><figcaption>t={r["t"]}s · old: '
        f'{html.escape(", ".join(x["name"] for x in r["old"]) or "—")} · new: '
        f'{html.escape(", ".join((x["code"] or x["det_class"]) + " (" + x["decision"] + ")" for x in r["new"]) or "—")}'
        f'</figcaption></figure>' for r in rows)
    (out / "index.html").write_text(f"""<!doctype html><meta charset="utf-8"><title>Signs: old vs new</title>
<style>body{{font:14px system-ui;margin:16px;background:#f6f7f9}}figure{{margin:0 0 18px}}img{{width:100%;max-width:1460px;border:1px solid #ccc}}
pre{{background:#fff;padding:10px;border:1px solid #ddd}}</style>
<h1>Traffic signs — old model (left) vs new v1 (right)</h1><pre>{html.escape(json.dumps(summary, indent=1, ensure_ascii=False))}</pre>
{cards}""")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
