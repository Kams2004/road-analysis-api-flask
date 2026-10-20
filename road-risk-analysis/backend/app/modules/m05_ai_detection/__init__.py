"""
Module 5 — AI Hazard & Road Infrastructure Detection (RRAP-05).

  routes/ai_models.py   /models — model registry and live AI quality metrics
  model_registry.py     weights + model cards, crop helpers
  model_stats.py        live quality metrics of the AI validation
  ai_validation.py      single source of the automatic accept / reject / review decision
  static_check.py       video-consistency tracker and pothole false-positive rules
  sign_taxonomy.py      traffic-sign codes of the two-stage sign pipeline
  osd_parser.py         OCR of the dashcam overlay (time, GPS, speed …)
  parked_vehicle/       parked / roadside vehicle category (RRAP-05-001/003/015):
                        settings, pure rules, scoring, severity, processor stage

The video processor that runs these stages is the ingestion pipeline
(app/modules/m07_ingestion/pipeline.py).
"""
