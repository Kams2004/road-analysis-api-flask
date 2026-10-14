import cv2, numpy as np, pytesseract, re, sys

VIDEO = '/home/kamsu-perold/road-analysis-api/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)
if not cap.isOpened():
    print("Cannot open video"); sys.exit(1)

h_frame = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
w_frame = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
print(f"Frame size: {w_frame}x{h_frame}")

GPS_RE = re.compile(
    r"(\d{3,4}[.,]\d{3,4})\s*[,\s]\s*([NS])"
    r"\s*[,\s]\s*(\d{4,5}[.,]\d{3,4})\s*[,\s]\s*([EW])",
    re.IGNORECASE,
)

for frame_num in [25, 85, 155, 370, 660, 875]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    if not ret:
        continue
    h, w = frame.shape[:2]

    # Test three different y-ranges and x-starts
    for y0, y1 in [(18, 52), (22, 48), (25, 45)]:
        for x_ratio in [0.43, 0.46, 0.50]:
            roi = frame[y0:y1, int(x_ratio*w):]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            big = cv2.resize(gray, (gray.shape[1]*3, gray.shape[0]*3),
                             interpolation=cv2.INTER_CUBIC)
            _, binary = cv2.threshold(big, 0, 255,
                                      cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            if np.sum(binary > 0) / binary.size > 0.5:
                binary = cv2.bitwise_not(binary)

            cfg = '--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789.,NSEWKM/H '
            text = pytesseract.image_to_string(binary, config=cfg).strip()
            match = GPS_RE.search(text)
            status = "MATCH" if match else "no match"
            print(f"f={frame_num:5d} y={y0}:{y1} x={x_ratio}: [{text!r}] -> {status}")
    print()

cap.release()
print("Done")
