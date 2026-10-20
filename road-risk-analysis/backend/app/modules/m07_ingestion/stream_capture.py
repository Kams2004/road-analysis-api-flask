"""
Live stream access with OpenCV (RRAP-07-003/011): a connection test and the
capture of one fixed-length segment to an mp4 that is then processed as an
ordinary job. Blocking calls — run them in a worker or a threadpool.
"""
import logging
import os
import time
from dataclasses import dataclass

import cv2

logger = logging.getLogger(__name__)

# Fail fast on unreachable RTSP/HTTP sources instead of FFmpeg's ~30 s default.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;10000000")
OPEN_TIMEOUT_MS = 10000
READ_TIMEOUT_MS = 10000
MAX_READ_FAILURES = 50


class StreamError(Exception):
    pass


@dataclass
class Segment:
    path: str
    frames: int
    fps: float
    width: int
    height: int


def _open(url: str) -> "cv2.VideoCapture":
    params = []
    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS]
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params) if params else cv2.VideoCapture(url)
    except Exception:  # older OpenCV without the params overload
        cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        raise StreamError(f"Cannot open stream {url}")
    return cap


def test_stream(url: str) -> dict:
    """Open the stream and read one frame → {ok, error?, width?, height?}."""
    try:
        cap = _open(url)
    except StreamError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            return {"ok": False, "error": "Stream opened but no frame could be read"}
        h, w = frame.shape[:2]
        return {"ok": True, "width": int(w), "height": int(h)}
    finally:
        cap.release()


def capture_segment(url: str, seconds: int, dest: str) -> Segment:
    """Record `seconds` of wall-clock video from `url` into `dest` (mp4)."""
    cap = _open(url)
    writer = None
    frames = 0
    failures = 0
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        if not fps or fps > 120 or fps != fps:
            fps = 25.0
        width = height = 0
        deadline = time.monotonic() + seconds
        # A file source (or a fast HTTP source) delivers frames faster than
        # real time — also stop once a segment's worth of frames is written.
        max_frames = int(fps * seconds)
        while time.monotonic() < deadline and frames < max_frames:
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= MAX_READ_FAILURES or not cap.isOpened():
                    break
                time.sleep(0.05)
                continue
            if writer is None:
                height, width = frame.shape[:2]
                writer = cv2.VideoWriter(dest, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
                if not writer.isOpened():
                    raise StreamError(f"Cannot write segment file {dest}")
            writer.write(frame)
            frames += 1
    finally:
        cap.release()
        if writer is not None:
            writer.release()
    if frames == 0:
        try:
            os.unlink(dest)
        except FileNotFoundError:
            pass
        raise StreamError("No frame received from the stream")
    return Segment(dest, frames, float(fps), width, height)
