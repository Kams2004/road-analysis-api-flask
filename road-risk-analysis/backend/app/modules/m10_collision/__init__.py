"""
Module 10 — Predictive Cooperative Collision Prevention (RRAP-10).

Ping-triggered pipeline: telemetry (vehicle_service) → road-aware state
(road_state_service, OSRM map matching) → trajectory prediction →
collision detection (physics, layer 1) → context modulation (layer 2) →
alerts → WebSocket / polling. Design: docs/COLLISION-PREVENTION.md.

  routes/                /vehicles, /road-state, /trajectory, /collisions, /alerts (+ WS /alerts/ws,
                         feedback), /collision/metrics
  vehicle_service.py     live telemetry in Redis (speed / heading smoothed over 3 pings)
  road_state_service.py  OSRM map matching of vehicles (RoadAwareVehicleState)
  trajectory_service.py + trajectory/   trajectory prediction, smoothing
  collision_service.py  + collision/    pairwise collision risk, thresholds, Context Risk Index
  context_service.py     context map precomputed into Redis (beat task) + hot-path lookups
  alert_service.py      + alerts/       alert engine, pub/sub fan-out, notifier
  metrics_service.py     hourly alert counters, latency, driver feedback
  staleness_guard.py     out-of-order write protection of the background pipeline
  models/                vehicles, vehicle road state, alert feedback
  schemas.py             vehicle telemetry, feedback and metrics schemas
"""
