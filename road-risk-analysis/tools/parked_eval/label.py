"""
Labelling helper: turn tracks of an evaluate.py run (usually the permissive
candidate run, `--tag cand --settings candidate_settings.json --sheets`) into
labels/<segment>.json after looking at the contact sheets.

    python tools/parked_eval/label.py SEG --tag cand 0=parked 3=oncoming 5=moving_same_dir:"note" ...
    python tools/parked_eval/label.py SEG --tag cand --list

Track numbers are the `#n` printed on the sheets. Labelling the same vehicle
twice (two tracks after an ID break) is fine: give both the same label. The
labels file is rewritten for the tracks given (existing labels of other
tracks are kept unless --replace).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = ("parked", "moving_same_dir", "oncoming", "queue", "crossing", "ignore")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("segment")
    ap.add_argument("items", nargs="*", help="N=label[:note]")
    ap.add_argument("--tag", default="cand")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--replace", action="store_true")
    args = ap.parse_intermixed_args()
    tracks = json.loads((HERE / "out" / args.tag / "tracks.json").read_text())[args.segment]
    if args.list:
        for i, t in enumerate(tracks):
            s = t["sightings"]
            print(f"#{i} {t['verdict']:22s} {t['class']:6s} n={len(s):3d} f{s[0][0]}-{s[-1][0]} "
                  f"area={t['features']['closest_area']:.4f} truth={t.get('truth')}")
        return
    path = HERE / "labels" / f"{args.segment}.json"
    current = json.loads(path.read_text())["vehicles"] if path.exists() and not args.replace else []
    for item in args.items:
        num, rest = item.split("=", 1)
        label, _, note = rest.partition(":")
        if label not in LABELS:
            raise SystemExit(f"unknown label {label!r}; allowed {LABELS}")
        t = tracks[int(num)]
        sightings = [s[:5] for s in t["sightings"]]
        frames = {s[0] for s in sightings}
        same = [v for v in current if frames & {s[0] for s in v["sightings"]} and _same(v["sightings"], sightings)]
        for v in same:                         # the same vehicle (an earlier track of it): merge
            current.remove(v)
            seen = {s[0] for s in sightings}
            sightings = sorted(sightings + [s for s in v["sightings"] if s[0] not in seen])
            note = note or v.get("note", "")
        current.append({"label": label, "note": note, "class": t["class"], "sightings": sightings})
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"segment": args.segment, "vehicles": current}, indent=0))
    print(f"{path.name}: {len(current)} labelled vehicles")


def _same(a, b) -> bool:
    """Two sighting lists describe the same vehicle (a shared frame with IoU ≥ 0.5)."""
    bb = {s[0]: s[1:] for s in b}
    for fr, *box in a:
        o = bb.get(fr)
        if o:
            ix = max(0, min(box[2], o[2]) - max(box[0], o[0]))
            iy = max(0, min(box[3], o[3]) - max(box[1], o[1]))
            union = (box[2] - box[0]) * (box[3] - box[1]) + (o[2] - o[0]) * (o[3] - o[1]) - ix * iy
            if union > 0 and ix * iy / union >= 0.5:
                return True
    return False


if __name__ == "__main__":
    main()
