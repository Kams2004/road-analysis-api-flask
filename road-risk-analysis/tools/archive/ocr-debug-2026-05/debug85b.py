import sys
sys.path.insert(0, '/app')
import cv2, numpy as np, pytesseract
from app.services.osd_parser import _crop, ROI_GPS, _preprocess_variants, _TESS_GPS, _norm, _LON_RE, _nmea_to_decimal

VIDEO = '/app/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)
cap.set(cv2.CAP_PROP_POS_FRAMES, 85)
ret, frame = cap.read()
cap.release()

gps_roi = _crop(frame, ROI_GPS)
all_candidates = []
for variant in _preprocess_variants(gps_roi):
    for psm in [6, 7]:
        cfg = _TESS_GPS.replace('--psm 6', '--psm %d' % psm)
        text = pytesseract.image_to_string(variant, config=cfg).strip()
        if text:
            all_candidates.append(text)

print("Total candidates: %d" % len(all_candidates))
for raw in all_candidates:
    text = _norm(raw)
    m = _LON_RE.search(text)
    if m:
        lon_dir = m.group(2).upper()
        if lon_dir not in ('E', 'W'):
            lon_dir = 'E'
        lon_str = m.group(1).replace(',', '.').replace(' ', '')
        if len(lon_str.split('.')[0]) == 4:
            lon_str = '0' + lon_str
        try:
            v = _nmea_to_decimal(lon_str, lon_dir)
            print("  raw=%r" % raw[:50])
            print("  normed=%r match=%r lon_str=%s v=%.6f in_range=%s" % (
                text[:50], m.group(0), lon_str, v, 8.0 <= v <= 16.0))
        except Exception as e:
            print("  error: %s" % e)
