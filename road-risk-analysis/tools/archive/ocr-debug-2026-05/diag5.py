import sys, re
sys.path.insert(0, '/app')
import cv2, numpy as np, pytesseract

VIDEO = '/app/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)

GPS_RE = re.compile(
    r"(\d{3,4}[.,]\s*\d{3,4})\s*[,\s]\s*([NS])\s*[,\s]\s*(\d{4,5}[.,]\s*\d{3,4})\s*[,\s]\s*([EW06CGO])",
    re.IGNORECASE,
)

from app.services.osd_parser import ROI_GPS, _crop

for frame_num in [25, 85, 660]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    if not ret:
        continue
    h, w = frame.shape[:2]
    print("\n=== frame %d ===" % frame_num)

    roi = _crop(frame, ROI_GPS)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

    kernel = np.array([[-1,-1,-1],[-1,9,-1],[-1,-1,-1]], dtype=np.float32)
    sharp = cv2.filter2D(gray, -1, kernel)

    for scale in [3, 4]:
        for src, sname in [(gray, "plain"), (sharp, "sharp")]:
            big = cv2.resize(src, (src.shape[1]*scale, src.shape[0]*scale), interpolation=cv2.INTER_CUBIC)
            for thresh in [160, 170, 180, 190, 200]:
                _, bt = cv2.threshold(big, thresh, 255, cv2.THRESH_BINARY)
                for psm in [6, 7, 13]:
                    cfg = '--psm %d --oem 1 -c tessedit_char_whitelist=0123456789.,NSEWKM/H ' % psm
                    text = pytesseract.image_to_string(bt, config=cfg).strip()
                    m = GPS_RE.search(text)
                    if m:
                        print("  MATCH scale=%d %s thresh=%d psm=%d: [%r]" % (scale, sname, thresh, psm, text))
                    elif '515' in text and ('N' in text or 'n' in text):
                        print("  near  scale=%d %s thresh=%d psm=%d: [%r]" % (scale, sname, thresh, psm, text))

cap.release()
