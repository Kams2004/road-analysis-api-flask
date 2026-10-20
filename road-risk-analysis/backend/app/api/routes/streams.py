"""
Stream sources (RRAP-07-003/011): live cameras captured in segments of
`segment_seconds`, each segment processed as a regular job (source_type
stream). Capture is driven by Celery beat (app.workers.job_tasks.capture_streams)
for every stream in status `active`.
"""
import asyncio
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import Principal, actor_name, get_principal
from app.api.openapi import errors, legacy
from app.db.session import get_db
from app.models.stream_source import StreamSource, StreamStatus
from app.services import audit, job_service

router = APIRouter()

URL_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "rtmps://", "http://", "https://", "udp://", "srt://")
TEST_TIMEOUT_S = 20


def _check_url(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip()
    if not v.lower().startswith(URL_SCHEMES):
        raise ValueError(f"URL must start with one of: {', '.join(URL_SCHEMES)}")
    return v


def _check_models(v: Optional[list[str]]) -> Optional[list[str]]:
    if v is None:
        return v
    unknown = set(v) - set(job_service.VALID_MODELS)
    if unknown:
        raise ValueError(f"Unknown model(s): {sorted(unknown)}")
    return sorted(set(v))


_URL_DESC = "Stream URL; scheme one of " + ", ".join(f"`{s}`" for s in URL_SCHEMES) + "."
_MODELS_DESC = "Models to run on each segment: " + ", ".join(f"`{m}`" for m in job_service.VALID_MODELS) + "."


class StreamIn(BaseModel):
    """A new live stream source (created `paused`)."""
    name:            str = Field(min_length=1, max_length=120, description="Display name.",
                                 examples=["Akwa roundabout camera"])
    url:             str = Field(description=_URL_DESC, examples=["rtsp://10.0.0.12:554/stream1"])
    camera_type:     Optional[str] = Field(None, description="Free text, e.g. `dashcam`, `fixed`.", examples=["fixed"])
    segment_seconds: int = Field(60, ge=10, le=1800, description="Length of each recorded segment, in seconds.")
    frame_skip:      Optional[int] = Field(None, ge=1, le=60, description="Process one frame out of N (default: "
                                                                          "the platform's stream sampling).")
    enabled_models:  list[str] = Field(default_factory=lambda: list(job_service.DEFAULT_MODELS),
                                       description=_MODELS_DESC + " Default: all.", examples=[["pothole", "signs"]])

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        return _check_url(v)

    @field_validator("enabled_models")
    @classmethod
    def _models(cls, v):
        return _check_models(v)


class StreamPatch(BaseModel):
    """Partial update: only the fields sent are changed. `null` clears `camera_type` and `frame_skip`
    and is ignored for the other fields."""
    name:            Optional[str] = Field(None, min_length=1, max_length=120, description="Display name.")
    url:             Optional[str] = Field(None, description=_URL_DESC)
    camera_type:     Optional[str] = Field(None, description="Free text (`null` clears it).")
    segment_seconds: Optional[int] = Field(None, ge=10, le=1800, description="Segment length, in seconds.")
    frame_skip:      Optional[int] = Field(None, ge=1, le=60, description="Process one frame out of N (`null` = default).")
    enabled_models:  Optional[list[str]] = Field(None, description=_MODELS_DESC)

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        return _check_url(v)

    @field_validator("enabled_models")
    @classmethod
    def _models(cls, v):
        return _check_models(v)


class StreamOut(BaseModel):
    """A live stream source."""
    id:                 str = Field(description="Stream id (UUID).")
    name:               str = Field(description="Display name.", examples=["Akwa roundabout camera"])
    url:                str = Field(description="Stream URL.", examples=["rtsp://10.0.0.12:554/stream1"])
    camera_type:        Optional[str] = Field(description="Free-text camera type.")
    status:             str = Field(description="`active` (segments are recorded), `paused`, or `error` (last "
                                                "capture failed, see `last_error`).", examples=["active"])
    segment_seconds:    int = Field(description="Segment length, in seconds.", examples=[60])
    frame_skip:         Optional[int] = Field(description="Process one frame out of N (`null` = default).")
    enabled_models:     list[str] = Field(description="Models run on each segment.", examples=[["pothole", "signs"]])
    last_segment_at:    Optional[datetime] = Field(description="When the last segment was captured (UTC).")
    last_error:         Optional[str] = Field(description="Last capture error.")
    segments_processed: int = Field(description="Number of segments turned into jobs.", examples=[128])
    created_by:         Optional[str] = Field(description="Creator.")
    created_at:         datetime = Field(description="Creation time (UTC).")

    class Config:
        from_attributes = True


class StreamTestOut(BaseModel):
    """Result of a connection test. On success `width`/`height` are present, on failure `error`."""
    ok:     bool = Field(description="Whether a frame could be read.")
    error:  Optional[str] = Field(None, description="Why the test failed (only when `ok` is false).",
                                  examples=["No answer from the stream within 20s"])
    width:  Optional[int] = Field(None, description="Frame width in pixels (only when `ok` is true).", examples=[1920])
    height: Optional[int] = Field(None, description="Frame height in pixels (only when `ok` is true).", examples=[1080])


StreamId = Annotated[str, Path(description="Stream id (UUID).")]
E_NOT_FOUND = legacy(404, "Stream not found")


def _snapshot(s: StreamSource) -> dict:
    return {k: getattr(s, k) for k in ("name", "url", "camera_type", "status", "segment_seconds",
                                       "frame_skip", "enabled_models")}


async def _get(db: AsyncSession, stream_id: str) -> StreamSource:
    s = await db.get(StreamSource, stream_id)
    if s is None:
        raise HTTPException(404, "Stream not found")
    return s


@router.get("/", response_model=list[StreamOut], summary="List streams")
async def list_streams(db: AsyncSession = Depends(get_db)):
    """Every stream source, newest first."""
    return (await db.execute(select(StreamSource).order_by(StreamSource.created_at.desc()))).scalars().all()


@router.post("/", response_model=StreamOut, status_code=201, summary="Register a stream")
async def create_stream(body: StreamIn, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
    """Register a live camera stream (RRAP-07-003). It is created `paused`: call
    `POST /streams/{stream_id}/start` to begin recording. Audited as `stream.create`."""
    s = StreamSource(**body.model_dump(), status=StreamStatus.paused.value, segments_processed=0,
                     created_by=actor_name(principal, principal.label))
    db.add(s)
    await db.flush()
    audit.record(db, request, "stream.create", principal=principal, object_type="stream", object_id=s.id,
                 details=_snapshot(s))
    await db.commit()
    await db.refresh(s)
    return s


@router.get("/{stream_id}", response_model=StreamOut, summary="Get a stream", responses=errors(E_NOT_FOUND))
async def get_stream(stream_id: StreamId, db: AsyncSession = Depends(get_db)):
    """One stream source with its capture status."""
    return await _get(db, stream_id)


@router.patch("/{stream_id}", response_model=StreamOut, summary="Update a stream", responses=errors(E_NOT_FOUND))
async def update_stream(stream_id: StreamId, body: StreamPatch, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
    """Change the fields sent; applies from the next segment. Audited as `stream.update`."""
    s = await _get(db, stream_id)
    before = _snapshot(s)
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None and key not in ("camera_type", "frame_skip"):
            continue
        setattr(s, key, value)
    audit.record(db, request, "stream.update", principal=principal, object_type="stream", object_id=s.id,
                 details=audit.diff(before, _snapshot(s)))
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/{stream_id}", status_code=204, summary="Delete a stream", responses=errors(E_NOT_FOUND))
async def delete_stream(stream_id: StreamId, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
    """Remove the stream source. Jobs of past segments stay (their `stream_id` is cleared). Audited as
    `stream.delete`."""
    s = await _get(db, stream_id)
    audit.record(db, request, "stream.delete", principal=principal, object_type="stream", object_id=s.id,
                 details=_snapshot(s))
    await db.delete(s)
    await db.commit()


async def _set_status(db: AsyncSession, request: Request, principal: Principal, stream_id: str,
                      status: StreamStatus, action: str) -> StreamSource:
    s = await _get(db, stream_id)
    before = s.status
    s.status = status.value
    if status == StreamStatus.active:
        s.last_error = None
    audit.record(db, request, f"stream.{action}", principal=principal, object_type="stream", object_id=s.id,
                 details={"from": before, "to": s.status})
    await db.commit()
    await db.refresh(s)
    return s


@router.post("/{stream_id}/start", response_model=StreamOut, summary="Start recording a stream",
             responses=errors(E_NOT_FOUND))
async def start_stream(stream_id: StreamId, request: Request, db: AsyncSession = Depends(get_db),
                       principal: Principal = Depends(get_principal)):
    """Status → `active` (whatever it was; `last_error` is cleared). Capture begins within a minute
    (next scheduler tick); each segment becomes a job with `source_type` `stream`. Audited as
    `stream.start`."""
    return await _set_status(db, request, principal, stream_id, StreamStatus.active, "start")


@router.post("/{stream_id}/pause", response_model=StreamOut, summary="Pause a stream",
             responses=errors(E_NOT_FOUND))
async def pause_stream(stream_id: StreamId, request: Request, db: AsyncSession = Depends(get_db),
                       principal: Principal = Depends(get_principal)):
    """Status → `paused`. The segment being recorded is finished and processed; no new one starts.
    Audited as `stream.pause`."""
    return await _set_status(db, request, principal, stream_id, StreamStatus.paused, "pause")


@router.post("/{stream_id}/test", response_model=StreamTestOut, response_model_exclude_unset=True,
             summary="Test a stream's connection", responses=errors(E_NOT_FOUND))
async def test_stream(stream_id: StreamId, db: AsyncSession = Depends(get_db)):
    """
    Open the stream and read one frame (timeout 20 s). Always answers 200: `{"ok": true, "width",
    "height"}` on success, `{"ok": false, "error"}` otherwise. Changes nothing.
    """
    s = await _get(db, stream_id)
    from app.services.stream_capture import test_stream as probe
    try:
        return await asyncio.wait_for(run_in_threadpool(probe, s.url), TEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"No answer from the stream within {TEST_TIMEOUT_S}s"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
