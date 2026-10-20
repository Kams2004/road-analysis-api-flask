import sys
sys.path.insert(0, '/app')
import cv2, numpy as np, pytesseract
from app.services.osd_parser import _crop, ROI_GPS, _preprocess_variants, _TESS_GPS, parse_gps_text

VIDEO = '/app/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)

for frame_num in [25, 85, 155, 660, 875]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    if not ret:
        continue
    print("\n=== frame %d ===" % frame_num)
    gps_roi = _crop(frame, ROI_GPS)
    best = ""
    for i, variant in enumerate(_preprocess_variants(gps_roi)):
        for psm in [6, 7]:
            cfg = _TESS_GPS.replace('--psm 6', '--psm %d' % psm)
            text = pytesseract.image_to_string(variant, config=cfg).strip()
            lat, lon, speed = parse_gps_text(text)
            if lat is not None:
                print("  variant=%d psm=%d MATCH: [%r] lat=%s lon=%s" % (i, psm, text, lat, lon))
            elif '515' in text and len(text) > 10:
                print("  variant=%d psm=%d near:  [%r]" % (i, psm, text))

cap.release()
