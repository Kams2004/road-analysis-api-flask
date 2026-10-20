"""
Module 10 — Predictive Cooperative Collision Prevention (RRAP-10).

Ping-triggered pipeline: telemetry (vehicle_service) → road-aware state
(road_state_service, OSRM map matching) → trajectory prediction →
collision detection → alerts.

  routes/                /vehicles, /road-state, /trajectory, /collisions, /alerts
  vehicle_service.py     live telemetry in Redis
  road_state_service.py  OSRM map matching of vehicles (RoadAwareVehicleState)
  trajectory_service.py + trajectory/   trajectory prediction
  collision_service.py  + collision/    pairwise collision risk and thresholds
  alert_service.py      + alerts/       alert engine and notifier
  staleness_guard.py     out-of-order write protection of the background pipeline
  models/                vehicles, vehicle road state
  schemas.py             vehicle telemetry schemas
"""
