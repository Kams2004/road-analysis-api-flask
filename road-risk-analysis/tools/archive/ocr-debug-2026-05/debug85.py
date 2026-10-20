import sys
sys.path.insert(0, '/app')
import re
from app.modules.m05_ai_detection.osd_parser import _norm, _LON_RE, _nmea_to_decimal

# Simulate what _parse_partial_gps does for frame 85 candidates
# From diag7: frame 85 near-matches
candidates = [
    '0515 53440 2N,01013. 554',
    '0 S1S\n3440\n,\n. /',
    'ESEE3440,N,01013.554S7M ,\n2 4',
    'EE3440,N,01013.554SEN',
    'SEEE3440,N,01013.554SE',
]

for raw in candidates:
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
            print("raw=%r -> text=%r -> match=%r dir=%s lon_str=%s -> v=%s in_range=%s" % (
                raw[:40], text[:40], m.group(0), lon_dir, lon_str, v, 8.0 <= v <= 16.0
            ))
        except Exception as e:
            print("error: %s" % e)
