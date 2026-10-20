"""
Module 7 — Video/Data Ingestion & Processing Jobs (RRAP-07).

  routes/jobs.py, streams.py,          /jobs, /streams, /cloud-connections,
  cloud_connections.py, cloud_imports.py   /imports
  pipeline.py           the video / image processor run by the Celery task
                        process_video (app/workers/job_tasks.py): frame sampling,
                        detectors, AI validation, geolocation, detections
  job_service.py        job creation, retries, error classification
  job_slots.py          processing capacity (Redis slots)
  watchdog.py           stale-job watchdog (RRAP-07-010)
  stream_capture.py     segment capture of live streams
  cloud_import.py, cloud_providers.py, cloud_storage.py   cloud drives and imports
  sftp_core.py, sftp_source.py                            SFTP input sources
  models/               jobs, job events / track points, cloud connections,
                        import batches, stream sources
  schemas/              jobs.py, cloud_imports.py
"""
