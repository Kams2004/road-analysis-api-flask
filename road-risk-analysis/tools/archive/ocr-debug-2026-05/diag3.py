import cv2, numpy as np, pytesseract, re
import sys

VIDEO = '/app/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)
h_total = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
w_total = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
print("Video: %dx%d" % (w_total, h_total))

# Looser GPS regex - handles OCR noise in lat/lon digits
GPS_LOOSE = re.compile(
    r"(\d[\d.,]{5,10})\s*[,\s]\s*([NS])\s*[,\s]\s*(\d[\d.,]{6,11})\s*[,\s]\s*([EW])",
    re.IGNORECASE,
)

for frame_num in [25, 85, 660, 875]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    if not ret:
        continue
    h, w = frame.shape[:2]
    print("\n=== frame %d ===" % frame_num)

    # Try different ROI heights and x-starts for the GPS row
    for (y0, y1) in [(20, 55), (18, 58), (22, 52), (15, 60)]:
        for x0_ratio in [0.45, 0.48, 0.50, 0.52]:
            x0 = int(x0_ratio * w)
            roi = frame[y0:y1, x0:]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

            # Try multiple scale factors
            for scale in [3, 4]:
                big = cv2.resize(gray, (gray.shape[1]*scale, gray.shape[0]*scale),
                                 interpolation=cv2.INTER_CUBIC)
                # Sharpen
                kernel = np.array([[-1,-1,-1],[-1,9,-1],[-1,-1,-1]])
                sharp = cv2.filter2D(big, -1, kernel)

                for img, iname in [(big, "plain"), (sharp, "sharp")]:
                    # Fixed threshold at 180 (white text)
                    _, bt = cv2.threshold(img, 180, 255, cv2.THRESH_BINARY)
                    for psm in [7, 6, 13]:
                        cfg = '--psm %d --oem 1 -c tessedit_char_whitelist=0123456789.,NSEWKM/H ' % psm
                        text = pytesseract.image_to_string(bt, config=cfg).strip()
                        m = GPS_LOOSE.search(text)
                        if m or ('N' in text and 'E' in text and len(text) > 8):
                            print("  y=%d:%d x=%.2f scale=%d %s psm=%d: [%r] -> %s" % (
                                y0, y1, x0_ratio, scale, iname, psm, text,
                                'MATCH lat=%s lon=%s' % (m.group(1), m.group(3)) if m else 'partial'
                            ))

cap.release()
