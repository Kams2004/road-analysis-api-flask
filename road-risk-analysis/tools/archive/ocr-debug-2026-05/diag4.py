import sys, re
sys.path.insert(0, '/app')
import cv2
from app.services.osd_parser import _norm, _GPS_RE, _preprocess, _crop, _ocr, ROI_GPS, _TESS_GPS

VIDEO = '/app/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)

for frame_num in [25, 85, 155, 660, 875]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    if not ret:
        continue
    raw = _ocr(_preprocess(_crop(frame, ROI_GPS)), _TESS_GPS)
    normed = _norm(raw)
    m = _GPS_RE.search(normed)
    print("f=%d raw=%r normed=%r match=%s" % (
        frame_num, raw, normed,
        (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None
    ))
    # Also try without whitelist to see what E/W looks like
    import pytesseract, numpy as np
    roi = _crop(frame, ROI_GPS)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    kernel = np.array([[-1,-1,-1],[-1,9,-1],[-1,-1,-1]], dtype=np.float32)
    sharp = cv2.filter2D(gray, -1, kernel)
    h, w = sharp.shape
    big = cv2.resize(sharp, (w*4, h*4), interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(big, 180, 255, cv2.THRESH_BINARY)
    # No whitelist
    raw2 = pytesseract.image_to_string(binary, config='--psm 6 --oem 1').strip()
    print("  no-whitelist: %r" % raw2)

cap.release()
