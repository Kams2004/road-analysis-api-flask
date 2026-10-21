"""
Per-job camera self-calibration (horizon line + lens height) from the car
boxes the vehicles detector already produces — no labels, no OSD.

A car of height H_car at range r has its contact line (box bottom) at
    y_bottom = horizon + H_cam·f / r      and an apparent height  h = H_car·f / r
so  y_bottom = horizon + (H_cam / H_car) · h  : a straight line over all the
car boxes of the job, whatever their range. A trimmed least-squares fit gives
the horizon (intercept) and the lens height (slope × the car class height).
Dashcams of the same equipment are mounted at very different heights / pitches
(evaluation videos: horizon 0.37 … 0.65 of the frame on 720×288 `total`
cameras), so a fixed default horizon puts every range off by a large factor —
the main cause of parked vehicles judged "moving" and oncoming / followed
vehicles judged "parked".

The focal length is NOT observable this way (it cancels out); it stays the
calibration's vertical_fov_deg (per OSD layout / vehicle profile).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

KEEP = 0.7            # share of the points kept after each trimming round
ROUNDS = 4
MAX_POINTS = 3000     # most recent boxes kept
REFIT_EVERY = 25      # new boxes between two fits


@dataclass(frozen=True)
class Fit:
    horizon_y: float
    camera_height_m: float
    points: int


def trimmed_line(xs: Sequence[float], ys: Sequence[float]) -> Optional[tuple[float, float, int]]:
    """(intercept, slope, points kept) of y = a + b·x, dropping the worst residuals each round."""
    pts = list(zip(xs, ys))
    a = b = 0.0
    for _ in range(ROUNDS):
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
        pts = pts[:max(5, int(len(pts) * KEEP))]
    return a, b, len(pts)


class HorizonEstimator:
    """Collects (box height, box bottom) of confident, uncut car boxes and
    fits the horizon / lens height once `min_boxes` are known."""

    def __init__(self, min_boxes: int = 40, min_conf: float = 0.5, car_height_m: float = 1.5,
                 horizon_bounds: tuple[float, float] = (0.2, 0.8),
                 height_bounds: tuple[float, float] = (0.8, 4.0)):
        self.min_boxes, self.min_conf, self.car_height_m = min_boxes, min_conf, car_height_m
        self.horizon_bounds, self.height_bounds = horizon_bounds, height_bounds
        self.h: list[float] = []
        self.y: list[float] = []
        self._fit: Optional[Fit] = None
        self._since = 0

    def add(self, nbox: Sequence[float], conf: float, cls: str) -> None:
        """One normalised detector box (x1, y1, x2, y2) of the frame."""
        if cls != "car" or conf < self.min_conf:
            return
        x1, y1, x2, y2 = nbox
        h = y2 - y1
        if y2 >= 0.97 or h < 0.03 or x1 <= 0.002 or x2 >= 0.998 or y1 <= 0.002:   # cut by the frame / tiny
            return
        self.h.append(h)
        self.y.append(y2)
        if len(self.h) > MAX_POINTS:
            del self.h[0], self.y[0]
        self._since += 1

    def fit(self) -> Optional[Fit]:
        """Current fit (None until min_boxes, or when it is implausible)."""
        if len(self.h) < self.min_boxes:
            return None
        if self._fit is None or self._since >= REFIT_EVERY:
            self._since = 0
            line = trimmed_line(self.h, self.y)
            self._fit = None
            if line is not None:
                a, b, n = line
                cam_h = b * self.car_height_m
                if (self.horizon_bounds[0] <= a <= self.horizon_bounds[1]
                        and self.height_bounds[0] <= cam_h <= self.height_bounds[1]):
                    self._fit = Fit(round(a, 4), round(cam_h, 3), n)
        return self._fit
