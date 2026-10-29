"""
Speed / heading smoothing over the last few telemetry samples of a vehicle —
pure, no I/O (vehicle_service.record_ping keeps the samples in Redis and
stores the smoothed values as the live `speed_mps` / `heading`; the raw ones
stay available as `raw_speed_mps` / `raw_heading`).

GPS speed and heading jitter from fix to fix (multipath between buildings, a
phone in a pocket); a single bad fix used to feed straight into the
prediction. Smoothing must not hide a real change either — a vehicle braking
hard or turning has to be seen at once — so:

  speed    three consistent samples (monotonic: accelerating or braking) → the
           newest one, unchanged; otherwise (a spike) → the median
  heading  only samples of a moving vehicle (speed > stationary) count — the
           heading of a stopped phone is noise; a consistent turn (all steps
           the same way) → the newest; otherwise the weighted circular mean
           (weights 1, 2, 3 … oldest → newest); stopped now → the last heading
           seen while moving
Samples older than max_span_s before the newest one are ignored.
"""
import math
from datetime import datetime
from typing import List, Optional, Sequence, Tuple


def _signed_diff(a: float, b: float) -> float:
    """b − a in (−180, 180]."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def _monotonic(xs: Sequence[float]) -> bool:
    ups = all(b >= a for a, b in zip(xs, xs[1:]))
    downs = all(b <= a for a, b in zip(xs, xs[1:]))
    return ups or downs


def smooth_speed(speeds: Sequence[float]) -> Optional[float]:
    """Oldest → newest."""
    if not speeds:
        return None
    if len(speeds) < 3 or _monotonic(speeds):
        return speeds[-1]
    s = sorted(speeds)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def smooth_heading(headings: Sequence[float]) -> Optional[float]:
    """Oldest → newest, moving samples only."""
    if not headings:
        return None
    if len(headings) < 2:
        return headings[-1] % 360.0
    steps = [_signed_diff(a, b) for a, b in zip(headings, headings[1:])]
    if all(s >= 0 for s in steps) or all(s <= 0 for s in steps):
        return headings[-1] % 360.0
    x = y = 0.0
    for w, h in enumerate(headings, start=1):
        x += w * math.cos(math.radians(h))
        y += w * math.sin(math.radians(h))
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return headings[-1] % 360.0
    return math.degrees(math.atan2(y, x)) % 360.0


def smooth_motion(
    samples: Sequence[Tuple[datetime, Optional[float], Optional[float]]],
    n: int = 3,
    stationary_speed_mps: float = 1.0,
    max_span_s: float = 15.0,
) -> Tuple[Optional[float], Optional[float]]:
    """(speed, heading) from (ts, speed, heading) samples in any order (the newest is the
    current ping). Duplicated timestamps keep the last one given."""
    if not samples:
        return None, None
    by_ts = {}
    for s in samples:
        by_ts[s[0]] = s
    ordered = sorted(by_ts.values(), key=lambda s: s[0])
    newest_ts = ordered[-1][0]
    recent = [s for s in ordered if (newest_ts - s[0]).total_seconds() <= max_span_s][-n:]
    newest = recent[-1]

    speeds: List[float] = [s[1] for s in recent if s[1] is not None]
    speed = smooth_speed(speeds) if newest[1] is not None else None

    moving = [s[2] for s in recent if s[2] is not None and (s[1] is None or s[1] > stationary_speed_mps)]
    if newest[2] is None:
        heading = None
    elif newest[1] is not None and newest[1] <= stationary_speed_mps:
        heading = moving[-1] % 360.0 if moving else newest[2]
    else:
        heading = smooth_heading(moving) if moving else newest[2]
    return speed, (round(heading, 2) if heading is not None else None)
