import sys, re
sys.path.insert(0, '/app')
import cv2, numpy as np, pytesseract

VIDEO = '/app/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)

GPS_RE = re.compile(
    r"(\d{3,4}[.,]\s*\d{3,4})\s*[,\s]\s*([NS])\s*[,\s]\s*(\d{4,5}[.,]\s*\d{3,4})\s*[,\s]\s*([EW06CGO])",
    re.IGNORECASE,
)

from app.services.osd_parser import ROI_GPS, _crop, _preprocess, _ocr, _TESS_GPS

for frame_num in [660, 25, 85]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    if not ret:
        continue
    h, w = frame.shape[:2]
    print("\n=== frame %d (%dx%d) ===" % (frame_num, w, h))
    print("ROI_GPS:", ROI_GPS)
    x0, y0 = int(ROI_GPS[0]*w), int(ROI_GPS[1]*h)
    x1, y1 = int(ROI_GPS[2]*w), int(ROI_GPS[3]*h)
    print("Pixel coords: y=%d:%d x=%d:%d" % (y0, y1, x0, x1))

    # Current pipeline
    roi = _crop(frame, ROI_GPS)
    binary = _preprocess(roi)
    text = _ocr(binary, _TESS_GPS)
    m = GPS_RE.search(text)
    print("Current pipeline: [%r] -> %s" % (text, 'MATCH' if m else 'no'))

    # Replicate the exact successful config from diag3: y=20:55, x=0.45, scale=4, sharp, psm=6
    roi2 = frame[20:55, int(0.45*w):]
    gray2 = cv2.cvtColor(roi2, cv2.COLOR_BGR2GRAY)
    kernel = np.array([[-1,-1,-1],[-1,9,-1],[-1,-1,-1]], dtype=np.float32)
    sharp2 = cv2.filter2D(gray2, -1, kernel)
    big2 = cv2.resize(sharp2, (sharp2.shape[1]*4, sharp2.shape[0]*4), interpolation=cv2.INTER_CUBIC)
    _, bt2 = cv2.threshold(big2, 180, 255, cv2.THRESH_BINARY)
    text2 = pytesseract.image_to_string(bt2, config='--psm 6 --oem 1 -c tessedit_char_whitelist=0123456789.,NSEWKM/H ').strip()
    m2 = GPS_RE.search(text2)
    print("Diag3 config:    [%r] -> %s" % (text2, 'MATCH' if m2 else 'no'))

cap.release()
