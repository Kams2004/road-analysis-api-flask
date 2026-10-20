import sys
sys.path.insert(0, '/app')
import cv2, numpy as np, pytesseract

VIDEO = '/app/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)

for frame_num in [660, 25]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    h, w = frame.shape[:2]
    print("\n=== frame %d (%dx%d) ===" % (frame_num, w, h))

    # Save the full top strip to see all text
    strip = frame[15:65, :]
    cv2.imwrite('/app/temp/strip_f%d.png' % frame_num, strip)

    # Try full width for GPS row
    roi = frame[20:55, int(0.45*w):]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    big = cv2.resize(gray, (gray.shape[1]*3, gray.shape[0]*3), interpolation=cv2.INTER_CUBIC)
    _, bt = cv2.threshold(big, 170, 255, cv2.THRESH_BINARY)
    text = pytesseract.image_to_string(bt, config='--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789.,NSEWKM/H ').strip()
    print("  x=0.45 full: [%r]" % text)

    # Try with padding on right (extend beyond frame width using border)
    roi2 = frame[20:55, int(0.45*w):]
    padded = cv2.copyMakeBorder(roi2, 5, 5, 5, 20, cv2.BORDER_REPLICATE)
    gray2 = cv2.cvtColor(padded, cv2.COLOR_BGR2GRAY)
    big2 = cv2.resize(gray2, (gray2.shape[1]*3, gray2.shape[0]*3), interpolation=cv2.INTER_CUBIC)
    _, bt2 = cv2.threshold(big2, 170, 255, cv2.THRESH_BINARY)
    text2 = pytesseract.image_to_string(bt2, config='--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789.,NSEWKM/H ').strip()
    print("  padded:      [%r]" % text2)

cap.release()
print("Strips saved")
