"""
Hazard lifecycle (Module 6): an active hazard stays visible to drivers only
while the evidence says it is still there.

  settings.py    HazardLifecycleSettings — settings section `hazard_lifecycle`
  policy.py      effective policy of a (type, subtype, vehicle class) — pure
  timeutil.py    recording-time helpers (UTC-naive, local time) — pure
  events.py      hazard_events timeline rows
  sightings.py   re-sighting: a later detection near an ACTIVE hazard
  video_pass.py  "not seen" video passes → resolution `video_absence`
  votes.py       driver votes still_there / not_there → resolution `votes`
  expiry.py      expires_at, stale evidence, periodic expiry → `expiry`
  recurring.py   recurring spots (≥ N resolved hazards at the same place)
  service.py     orchestration: resolve / reopen / attach hooks / visibility

Nothing here is imported by app.services.hazard_service at module load
(only lazily inside functions) to keep the import graph acyclic.
"""
