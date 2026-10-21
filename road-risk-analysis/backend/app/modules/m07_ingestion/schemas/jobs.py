"""Response schemas of processing jobs (Module 7 — /jobs)."""
import os
from pydantic import BaseModel, Field, model_validator
from datetime import datetime
from typing import Optional, List
from app.modules.m07_ingestion.models.job import JobStatus


class JobOut(BaseModel):
    """A video/image processing job (Module 7). Statuses: `pending` (queued) → `processing` →
    `done` | `failed` | `cancelled`."""
    id:             str = Field(description="Job id (UUID).", examples=["7d0c5b9e-2f1a-4c3d-8e6b-1a2b3c4d5e6f"])
    filename:       str = Field(description="Original file name.", examples=["dashcam_2026-03-02_0958.mp4"])
    status:         JobStatus = Field(description="`pending`, `processing`, `done`, `failed` or `cancelled`.",
                                      examples=["processing"])
    enabled_models: Optional[str] = Field(description="Comma list of the models run (`pothole`, `signs`, "
                                                      "`speedbump`, `vehicles`).", examples=["pothole,signs"])
    total_frames:   int = Field(description="Frames in the media (0 until known).", examples=[5400])
    processed:      int = Field(description="Frames processed so far.", examples=[2700])
    detections:     int = Field(description="Detections produced so far.", examples=[14])
    error:          Optional[str] = Field(description="Failure reason (status `failed`).")
    created_at:     datetime = Field(description="Submission time (UTC).")
    finished_at:    Optional[datetime] = Field(description="End of processing (UTC), whatever the outcome.")

    # Module 7 — source, lifecycle and metrics
    source_type:         str                      = Field("upload", description="`upload`, `google_drive`, "
                                                          "`onedrive` or `stream`.")
    source_ref:          Optional[str]            = Field(None, description="Provider file id (cloud imports).")
    source_name:         Optional[str]            = Field(None, description="Source file / segment name.")
    connection_id:       Optional[str]            = Field(None, description="Cloud connection it was imported from.")
    stream_id:           Optional[str]            = Field(None, description="Stream it was recorded from.")
    submitted_by:        Optional[str]            = Field(None, description="User (or `system`) that created it.")
    media_retained:      bool                     = Field(False, description="The source file is still on disk "
                                                          "(needed to retry an upload).")
    started_at:          Optional[datetime]       = Field(None, description="Start of processing (UTC).")
    cancel_requested:    bool                     = Field(False, description="A cancel was requested while "
                                                          "processing; the worker stops at its next check.")
    cancelled_at:        Optional[datetime]       = Field(None, description="Cancellation time (UTC).")
    cancelled_by:        Optional[str]            = Field(None, description="Who cancelled it.")
    attempts:            int                      = Field(1, description="Current attempt number.")
    max_attempts:        int                      = Field(3, description="Retries are refused once `attempts` "
                                                          "reaches this.")
    avg_confidence:      Optional[float]          = Field(None, description="Mean confidence of its detections (0–1).")
    frame_skip:          Optional[int]            = Field(None, description="One frame out of N was processed.")
    detections_by_model: Optional[dict[str, int]] = Field(None, description="Detections per model.",
                                                          examples=[{"pothole": 10, "signs": 4}])
    fps:                 Optional[float]          = Field(None, description="Video frame rate.")
    duration_s:          Optional[float]          = Field(None, description="Video duration, in seconds.")
    osd_layout_requested: Optional[str]           = Field(None, description="Overlay layout asked at submission "
                                                          "(`null` = auto-detect / settings `osd`).")
    osd_layout:          Optional[str]            = Field(None, description="Overlay (OSD) layout used: a profile "
                                                          "(`total`, `besafe` …), `full_frame` (fallback) or `none` "
                                                          "(no overlay found). Set once detected.",
                                                          examples=["besafe"])
    processing_seconds:  Optional[float]          = Field(None, description="Processing time so far, in seconds "
                                                          "((finished_at or now) − started_at).")
    progress:            float                    = Field(0.0, description="processed / total_frames, 0–1.",
                                                          examples=[0.5])

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def _computed(cls, data):
        if isinstance(data, dict):
            return data
        from app.modules.m07_ingestion.job_service import progress, processing_seconds
        out = {name: getattr(data, name, None) for name in cls.model_fields
               if name not in ("processing_seconds", "progress", "media_retained")}
        out = {k: v for k, v in out.items() if v is not None or k in (
            "enabled_models", "error", "finished_at")}
        out["processing_seconds"] = processing_seconds(data)
        out["progress"] = progress(data)
        path = getattr(data, "source_path", None)
        out["media_retained"] = bool(path and os.path.exists(path))
        return out


class JobEventOut(BaseModel):
    """One lifecycle event of a job."""
    id:      int = Field(description="Event id.")
    at:      datetime = Field(description="When it happened (UTC).")
    event:   str = Field(description="`created`, `queued`, `started`, `cancel_requested`, `cancelled`, "
                                     "`failed`, `retried`, `done` …", examples=["queued"])
    actor:   Optional[str] = Field(description="User, or `system` for the worker.")
    details: Optional[dict] = Field(description="Event-specific data (task id, attempt, error …).",
                                    examples=[{"task_id": "b1f…", "attempt": 1}])

    class Config:
        from_attributes = True


class JobDetailOut(JobOut):
    """A job with its lifecycle events."""
    events: List[JobEventOut] = Field([], description="Lifecycle events, newest first.")


class JobListOut(BaseModel):
    """A page of jobs."""
    total: int = Field(description="Number of matching jobs (before paging).", examples=[57])
    items: List[JobOut] = Field(description="The jobs of this page.")


class JobCapacityOut(BaseModel):
    """Processing capacity and current load."""
    max_concurrent_jobs: int = Field(description="Jobs processed at the same time (setting "
                                                 "`processing.max_concurrent_jobs`).", examples=[2])
    running:             int = Field(description="Jobs in status `processing`.", examples=[1])
    pending:             int = Field(description="Jobs in status `pending` (queued).", examples=[3])
    worker_concurrency:  int = Field(description="Worker processes of the deployment.", examples=[2])


class JobSummaryOut(BaseModel):
    """Job counts per status (every status present)."""
    by_status: dict[str, int] = Field(description="All jobs, per status.",
                                      examples=[{"pending": 1, "processing": 1, "done": 40, "failed": 2,
                                                 "cancelled": 0}])
    last_24h:  dict[str, int] = Field(description="Jobs created in the last 24 hours, per status.",
                                      examples=[{"pending": 1, "processing": 1, "done": 5, "failed": 0,
                                                 "cancelled": 0}])
