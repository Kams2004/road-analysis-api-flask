"""
Module 6 — Hazard Management, Clustering, Validation & Evidence.

Hazards are persisted clusters of accepted AI detections. The clustering
itself (attach / detach / aggregates / rebuild) lives in
app.services.hazard_service; this package groups the submodules built on
top of it:

  lifecycle/   what happens to a hazard after it is opened — re-sightings in
               later videos, resolution after valid "not seen" video passes,
               driver votes, time-based expiry and recurring-spot detection
               (see lifecycle/__init__.py and docs/spec-module6-hazard-lifecycle.md).
"""
