"""
Box review in the server's Label Studio from the web console ("Training data"
page) — app.modules.m06_hazards.training_data.labelstudio, docs/TRAINING-DATA.md.

    router          /labelstudio/status, /labelstudio/{type}/send, /labelstudio/{type}/fetch
                    (permission module `datasets`)
    image_router    /labelstudio/image/{type}/{frame_key}.jpg?s=<signature> — public on
                    purpose: Label Studio (the annotator's browser) loads the task images
                    from it; only signed frame images are served.
"""
import asyncio

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated, Optional

from app.api.deps import Principal, actor_name, api_error, get_principal
from app.api.openapi import err, errors
from app.core import audit
from app.core.config import settings
from app.core.db.session import get_db
from app.modules.m06_hazards.routes.datasets import E_BAD_TYPE, _type
from app.modules.m06_hazards.training_data import labelstudio as ls
from app.modules.m06_hazards.training_data import layout

router = APIRouter()
image_router = APIRouter()

E_LS = err(503, "labelstudio_unavailable", "Label Studio is not reachable at http://labelstudio:8080 (…).")


class LsProjectOut(BaseModel):
    detection_type: str = Field(description="Detection type.", examples=["pothole"])
    project_id:     int = Field(description="Label Studio project id.")
    url:            str = Field(description="Project page in Label Studio (public address).")
    tasks:          int = Field(description="Tasks (frames) in the project.")
    annotated:      int = Field(description="Tasks with a submitted annotation.")
    skipped:        int = Field(description="Skipped annotations (not written back).")
    applied:        int = Field(description="Frames whose Label Studio boxes were written back.")


class LsStatusOut(BaseModel):
    configured: bool = Field(description="infra/secrets/labelstudio.env present with a token.")
    reachable:  bool = Field(description="The API reached Label Studio.")
    public_url: Optional[str] = Field(None, description="Label Studio address for the browser.")
    error:      Optional[str] = Field(None, description="Why Label Studio is not reachable.")
    projects:   list[LsProjectOut] = Field(description="One project per detection type sent so far.")


class LsSendOut(BaseModel):
    detection_type: str
    project_id:     int
    project_url:    str = Field(description="Open this page to review.")
    sent:           int = Field(description="New tasks created.")
    renewed:        int = Field(description="Frames reset by a later decision: their old task was replaced.")
    already_there:  int = Field(description="Frames already waiting in Label Studio.")
    not_ready:      int = Field(description="Frames whose image is not written yet (sent next time).")


class LsFetchOut(BaseModel):
    detection_type: str
    project_id:     int
    applied:        int = Field(description="Frames updated with the Label Studio boxes (now box_reviewed).")
    unchanged:      int = Field(description="Annotations already written back.")
    skipped:        int = Field(description="Skipped tasks (ignored).")
    missing:        int = Field(description="Tasks whose frame left the training data.")
    errors:         list[str] = Field(description="Annotations that could not be applied (unknown class …).")


def _ls_error(exc: ls.LabelStudioError):
    return api_error(503, "labelstudio_unavailable", str(exc))


@router.get("/status", response_model=LsStatusOut, summary="Label Studio connection and projects")
async def ls_status(db: AsyncSession = Depends(get_db)):
    """Whether the server's Label Studio is configured / reachable and, per detection type, its
    project counters (tasks, annotated, written back)."""
    return await ls.status(db)


@router.post("/{detection_type}/send", response_model=LsSendOut, summary="Send the frames to review to Label Studio",
             responses=errors(E_BAD_TYPE, E_LS))
async def ls_send(
    detection_type: Annotated[str, Path(description="Detection type.", examples=["pothole"])],
    request: Request, db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal),
):
    """
    Creates the type's project ("VIASafe – <type>") if needed and one task per training frame still
    `needs_box_review` (its current boxes as pre-annotations). Re-running only adds the new frames.
    Audited as `dataset.labelstudio_send`.
    """
    det_type = _type(detection_type, required=True)
    try:
        out = await ls.send(db, det_type)  # type: ignore[arg-type]
    except ls.LabelStudioError as exc:
        raise _ls_error(exc)
    await audit.record_now(request, "dataset.labelstudio_send", principal=principal, object_type="dataset",
                           object_id=det_type, details={k: out[k] for k in ("sent", "renewed", "already_there")})
    return out


@router.post("/{detection_type}/fetch", response_model=LsFetchOut,
             summary="Write back the boxes corrected in Label Studio", responses=errors(E_BAD_TYPE, E_LS))
async def ls_fetch(
    detection_type: Annotated[str, Path(description="Detection type.", examples=["pothole"])],
    request: Request, db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal),
):
    """
    Every annotation submitted (or edited) in Label Studio since the last fetch replaces the boxes
    of its frame — `box_reviewed`, an empty annotation = reviewed background. Skipped tasks are
    ignored. Audited as `dataset.labelstudio_fetch`.
    """
    det_type = _type(detection_type, required=True)
    try:
        out = await ls.fetch(db, det_type, actor_name(principal) or principal.label or "console")  # type: ignore[arg-type]
    except ls.LabelStudioError as exc:
        raise _ls_error(exc)
    await audit.record_now(request, "dataset.labelstudio_fetch", principal=principal, object_type="dataset",
                           object_id=det_type, details={k: out[k] for k in ("applied", "unchanged", "skipped")})
    return out


@image_router.get("/image/{detection_type}/{frame_key}.jpg", summary="Frame image for Label Studio (signed)",
                  include_in_schema=False)
async def ls_image(detection_type: str, frame_key: str, s: str = Query("", description="Signature.")):
    if detection_type not in ls.TYPE_TITLES or not ls.check_signature(detection_type, frame_key, s):
        raise api_error(404, "not_found", "Image not found.")

    def read() -> bytes:
        from app.core.storage.minio_service import get_minio
        r = get_minio().get_object(settings.MINIO_BUCKET_DATASETS, layout.image_key(detection_type, frame_key))
        try:
            return r.read()
        finally:
            r.close()
            r.release_conn()
    try:
        data = await asyncio.to_thread(read)
    except Exception:
        raise api_error(404, "not_found", "Image not found.")
    return Response(content=data, media_type="image/jpeg", headers={
        "Cache-Control": "private, max-age=86400", "Access-Control-Allow-Origin": "*"})
