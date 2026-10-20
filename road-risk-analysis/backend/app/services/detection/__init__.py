"""
Per-category AI detection submodules of Module 5 (AI Hazard & Road
Infrastructure Detection).

Each sub-package groups everything one detection category needs beyond the
raw detector — settings, pure decision rules, scoring, severity — behind a
small public API the video processor (app/workers/processor.py) calls:

  parked_vehicle   parked / roadside vehicles (RRAP-05-001/003/015): stock
                   COCO-pretrained YOLOv8 vehicles + physics/rule decision
                   "is it parked?" (no training).

The older categories still live as flat modules and are expected to move
here as the package grows (future structure, not moved yet):
  pothole        → app.services.ai_validation + crop classifier in the processor
  traffic_sign   → app.services.sign_taxonomy + classifier in the processor
  video checks   → app.services.static_check (shared tracker / range model)
"""
