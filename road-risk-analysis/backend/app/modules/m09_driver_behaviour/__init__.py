"""
Module 9 — Driver Behaviour Analytics (functional Module 5, RRAP-09).

Analyses the driving-behaviour events (speeding, hard braking, hard
acceleration, harsh cornering) stored in `historical_events` — the single
event store shared with Module 8 (historical risk); nothing is duplicated.

  settings.py    settings section `driver_behaviour` (weights, severity, zones)
  analytics.py   pure aggregation: severity, scoring, rankings, trends, heatmap, zones
  service.py     database access (period / filter queries)
  routes/        /driver-behaviour endpoints
"""
