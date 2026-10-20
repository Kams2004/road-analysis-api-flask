import sys
sys.path.insert(0, '/app')
import cv2
from app.services.osd_parser import extract_osd

VIDEO = '/app/LTTR 458 AV_24_04 (2).mp4'
cap = cv2.VideoCapture(VIDEO)

for frame_num in [25, 85, 155, 660, 875]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    if not ret:
        continue
    osd = extract_osd(frame)
    print("f=%d raw=%r lat=%s lon=%s speed=%s vehicle=%s rpm=%s loc=%s" % (
        frame_num, osd.raw_gps_text,
        osd.latitude, osd.longitude, osd.speed_kmh,
        osd.vehicle_id, osd.rpm, osd.location_name
    ))

cap.release()
