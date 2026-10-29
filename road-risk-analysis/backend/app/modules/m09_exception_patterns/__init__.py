"""
Module 9 — Exception patterns (functional Module 5, RRAP-09; formerly "Driver behaviour").

Anonymous statistics of the fleet exceptions (speeding, harsh braking, hard acceleration,
harsh cornering … ) stored in `historical_events` — the single event store shared with
Module 8 (historical risk); nothing is duplicated. Nothing concerns a driver or a vehicle: the
events carry none (app/modules/m08_historical/anonymity.py), so there are no rankings,
scores or profiles — only what happens, where, when, how fast and from which provider.

  settings.py    settings section `driver_behaviour` (key kept for stored settings): severity
                 rules, zone criteria, default period, time zone
  analytics.py   pure helpers: period windows, time buckets, severity, and per-event
                 aggregations (reports, tests)
  views.py       the same aggregations as grouped reads (app/modules/m08_historical/aggregates.py)
  service.py     settings access and the stored data extent
  routes/        /exception-patterns endpoints
"""
