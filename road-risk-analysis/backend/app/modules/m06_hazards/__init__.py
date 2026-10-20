"""
Module 6 — Hazard Management, Clustering, Validation & Evidence (RRAP-06).

Hazards are persisted clusters of accepted AI detections.

  routes/               /detections (queue, review workflow, spatial queries),
                        /hazards, /rejection-reasons, /datasets, cluster
                        configuration (/clustering, /cluster-zones,
                        /cluster-config, /cluster-areas)
  detection_workflow.py validation state machine (accept / correct / reject / reopen)
  hazard_service.py     clustering of detections into hazards (attach / detach /
                        aggregates / rebuild)
  lifecycle/            what happens to a hazard after it is opened — re-sightings in
                        later videos, resolution after valid "not seen" video passes,
                        driver votes, time-based expiry and recurring-spot detection
                        (docs/spec-module6-hazard-lifecycle.md)
  clustering/           global Cluster Configuration: zones, boundaries, radius resolver
  training_data/        live training dataset written from review decisions
                        (docs/TRAINING-DATA.md)
  dataset_service.py    dataset exports
  models/               detection, review, label, hazard (+ lifecycle), cluster
                        zones / areas / config, rejection reasons, datasets,
                        training samples
  schemas.py            detection, review, hazard, cluster and dataset schemas
"""
