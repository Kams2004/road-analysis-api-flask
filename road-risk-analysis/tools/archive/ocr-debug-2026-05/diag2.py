import cv2, numpy as np, pytesseract, re

img = cv2.imread('/app/temp/roi_raw_f25.png')
print("Shape:", img.shape)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
print("Gray min=%d max=%d mean=%.1f" % (gray.min(), gray.max(), gray.mean()))

for lo, hi in [(0,50),(50,100),(100,150),(150,200),(200,255)]:
    count = int(np.sum((gray >= lo) & (gray < hi)))
    print("  [%d-%d]: %d pixels (%.1f%%)" % (lo, hi, count, 100*count/gray.size))

GPS_RE = re.compile(
    r"(\d{3,4}[.,]\d{3,4})\s*[,\s]\s*([NS])\s*[,\s]\s*(\d{4,5}[.,]\d{3,4})\s*[,\s]\s*([EW])",
    re.IGNORECASE,
)

print("\n-- white text (bright pixels) --")
for thresh in [180, 190, 200, 210, 220]:
    _, bt = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY)
    big = cv2.resize(bt, (bt.shape[1]*3, bt.shape[0]*3), interpolation=cv2.INTER_CUBIC)
    for psm in [6, 7]:
        text = pytesseract.image_to_string(big, config='--psm %d --oem 1 -c tessedit_char_whitelist=0123456789.,NSEWKM/H ' % psm).strip()
        m = GPS_RE.search(text)
        print("  thresh>%d psm=%d: [%r] -> %s" % (thresh, psm, text, 'MATCH' if m else 'no'))

print("\n-- dark text (inverted) --")
for thresh in [80, 100, 120, 150]:
    _, bt = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY_INV)
    big = cv2.resize(bt, (bt.shape[1]*3, bt.shape[0]*3), interpolation=cv2.INTER_CUBIC)
    for psm in [6, 7]:
        text = pytesseract.image_to_string(big, config='--psm %d --oem 1 -c tessedit_char_whitelist=0123456789.,NSEWKM/H ' % psm).strip()
        m = GPS_RE.search(text)
        print("  dark<%d psm=%d: [%r] -> %s" % (thresh, psm, text, 'MATCH' if m else 'no'))
