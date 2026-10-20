import sys
sys.path.insert(0, '/app')
from app.services.osd_parser import _norm, parse_gps_text

# Test the S1S.5212 case
tests = [
    'S1S.5212,N,1013S2E',
    '0515.3749,N,01013.5428, 0N',
    'S1S.5212,N,01013.5469,E,2M',
]
for t in tests:
    normed = _norm(t)
    lat, lon, speed = parse_gps_text(t)
    print("raw:    %r" % t)
    print("normed: %r" % normed)
    print("result: lat=%s lon=%s" % (lat, lon))
    print()
