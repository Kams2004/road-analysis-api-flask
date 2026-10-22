import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, func, select, update
from typing import Annotated, List, Optional

from app.api.deps import Principal, actor_name, api_error, get_principal
from app.api.openapi import err, errors, legacy
from app.core.config import settings
from app.modules.m06_hazards.models.training_sample import TrainingFrame, TrainingSample
from app.core import audit
from app.modules.m06_hazards.training_data import card_info
from app.modules.m06_hazards.training_data import export as td_export
from app.modules.m06_hazards.training_data import sync as training_sync

from app.core.db.session import get_db
from app.modules.m06_hazards.models.dataset import Dataset
from app.modules.m06_hazards.schemas import DatasetOut, DatasetExportIn
from app.modules.m06_hazards.dataset_service import export_dataset

router = APIRouter()

VALID_TYPES = {"pothole", "traffic_sign", "speed_bump"}


@router.post("/export", response_model=DatasetOut, status_code=201, summary="Build a dataset version",
             responses=errors(
                 legacy(400, "detection_type must be one of ['pothole', 'speed_bump', 'traffic_sign']"),
                 legacy(422, "No validated labels found for detection_type='pothole'")))
async def create_dataset(
    body: DatasetExportIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Snapshot every training label of a detection type into a new immutable dataset version (version
    number = previous + 1). The crop images are copied into the datasets bucket and a manifest and a
    metadata JSON file are written (`manifest_url`, `metadata_url`).
    """
    if body.detection_type not in VALID_TYPES:
        raise HTTPException(400, f"detection_type must be one of {sorted(VALID_TYPES)}")
    try:
        dataset = await export_dataset(body.detection_type, body.created_by, db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return dataset


@router.get("/", response_model=List[DatasetOut], summary="List dataset versions")
async def list_datasets(
    detection_type: Optional[str] = Query(None, description="`pothole`, `traffic_sign` or `speed_bump`.",
                                          examples=["pothole"]),
    db: AsyncSession = Depends(get_db),
):
    """Every dataset version, by detection type then newest version first."""
    q = select(Dataset).order_by(Dataset.detection_type, Dataset.version.desc())
    if detection_type:
        q = q.where(Dataset.detection_type == detection_type)
    rows = (await db.execute(q)).scalars().all()
    return rows


# ── Live training data (docs/TRAINING-DATA.md, app.modules.m06_hazards.training_data) ──

TRAINING_TYPES = ("pothole", "speed_bump", "traffic_sign", "parked_vehicle")
E_BAD_TYPE = err(422, "invalid_detection_type", "Unknown detection type 'foo'.", allowed=list(TRAINING_TYPES))
E_NO_FRAME = err(404, "not_found", "No training frame 'X' for pothole.")
E_BAD_BOXES = err(422, "invalid_boxes", "unknown class 'crack' (classes: ['pothole'])")


def _type(det_type: Optional[str], required: bool = False) -> Optional[str]:
    if det_type is None and not required:
        return None
    if det_type not in TRAINING_TYPES:
        raise api_error(422, "invalid_detection_type", f"Unknown detection type '{det_type}'.",
                        allowed=list(TRAINING_TYPES))
    return det_type


class TrainingTypeSummaryOut(BaseModel):
    """Live training data of one detection type."""
    detection_type:    str = Field(description="Detection type.", examples=["pothole"])
    classes:           list[str] = Field(description="Detector classes (index = YOLO class id).",
                                         examples=[["pothole"]])
    accepted:          int = Field(description="Active samples accepted as predicted.")
    corrected:         int = Field(description="Active samples accepted after a box / class / location correction.")
    rejected:          int = Field(description="Active rejected samples, every kind (false_positive ones are "
                                       "classifier negatives / detector background).")
    excluded:          int = Field(0, description="Rejections of kind `unusable`: kept for traceability, no files.")
    redundant_positive: int = Field(0, description="Rejections of kind `real_redundant`, filed as positive samples.")
    superseded:        int = Field(description="Samples of reopened detections (removed from the dataset).")
    by_human:          int = Field(description="Active samples decided by a validator.")
    by_system:         int = Field(description="Active samples decided automatically (AI validation mode).")
    classifier_crops:  int = Field(description="Crops written under `<type>/classifier/`.")
    crops_by_class:    dict[str, int] = Field(description="Crops per classifier folder.",
                                              examples=[{"pothole": 120, "not_pothole": 45}])
    detector_frames:   int = Field(description="Clean frames written under `<type>/detector/images/`.")
    positive_frames:   int = Field(description="Detector frames with at least one box.")
    background_frames: int = Field(description="Detector frames with an empty label (only rejections).")
    boxes:             int = Field(description="Boxes in the detector labels.")
    box_reviewed:      int = Field(description="Detector frames whose boxes were reviewed in Label Studio.")
    needs_box_review:  int = Field(description="Detector frames still labelled from the decisions only.")
    frames_without_image: int = Field(description="Decided frames without a clean frame (detections stored "
                                                  "before clean frames existed): classifier crop only.")
    sync_pending:      int = Field(description="Samples whose objects are not written yet.")
    sync_failed:       int = Field(description="Samples whose last write failed (retried by the beat task).")
    last_synced_at:    Optional[datetime] = Field(None, description="Last object write (UTC).")
    last_decision_at:  Optional[datetime] = Field(None, description="Last decision recorded (UTC).")


class TrainingSampleOut(BaseModel):
    """One decided detection in the training dataset (id = detection id)."""
    model_config = ConfigDict(extra="allow")
    id:               str = Field(description="Detection id (= sample id, crop file name).")
    detection_type:   str = Field(description="Detection type (after a class correction).", examples=["pothole"])
    decision:         str = Field(description="`accepted`, `corrected` or `rejected`.", examples=["corrected"])
    state:            str = Field(description="`active` or `superseded` (detection reopened).")
    decided_by:       str = Field(description="`human` or `system` (automatic decision).")
    reviewer:         Optional[str] = Field(None, description="Who decided.")
    reason_code:      Optional[str] = Field(None, description="Rejection reason code (rejected samples).")
    rejection_kind:   Optional[str] = Field(None, description="Kind of the rejection reason: `false_positive` "
                                            "(negative), `real_redundant` (positive) or `unusable` (excluded).")
    excluded:         bool = Field(False, description="`true` = not used for training (unusable rejection, no files).")
    corrections:      Optional[list[str]] = Field(None, description="What was corrected: `box`, `class`, "
                                                                    "`location`.", examples=[["box"]])
    class_name:       Optional[str] = Field(None, description="Detector class (`null` unless positive).")
    class_index:      Optional[int] = Field(None, description="YOLO class id in `<type>/detector/classes.txt`.")
    classifier_class: str = Field(description="Classifier folder of the crop.", examples=["not_pothole"])
    bbox_x1:          Optional[float] = Field(None, description="Effective box (the corrected one when corrected), "
                                                                "frame pixels.")
    bbox_y1:          Optional[float] = None
    bbox_x2:          Optional[float] = None
    bbox_y2:          Optional[float] = None
    box_corrected:    bool = Field(description="The validator moved / resized the box.")
    frame_key:        str = Field(description="Detector frame stem.", examples=["LTTR_458_AV_24_04_2-1a2b3c4d__z002__t00151200"])
    group_key:        str = Field(description="Leakage group (video + job + minute) for train/val/test splits.")
    job_id:           str = Field(description="Job (video) that produced the detection.")
    source_name:      Optional[str] = Field(None, description="Video file name.")
    review_status:    Optional[str] = Field(None, description="Box review of its frame: `needs_box_review` or "
                                                              "`box_reviewed`.")
    crop_url:         Optional[str] = Field(None, description="Crop download path (`/datasets/files/…`).")
    sync_status:      str = Field(description="`pending`, `synced` or `failed`.")


class TrainingSampleListOut(BaseModel):
    """A page of training samples."""
    total: int = Field(description="Samples matching the filters.")
    items: list[TrainingSampleOut]


class ReviewBoxIn(BaseModel):
    """One reviewed box, YOLO-normalised (0–1)."""
    class_name:  Optional[str] = Field(None, description="Class name (or give `class_index`).", examples=["pothole"])
    class_index: Optional[int] = Field(None, description="YOLO class id.")
    cx: float = Field(ge=0, le=1, description="Box centre x / image width.", examples=[0.41])
    cy: float = Field(ge=0, le=1, description="Box centre y / image height.", examples=[0.78])
    w:  float = Field(gt=0, le=1, description="Box width / image width.", examples=[0.12])
    h:  float = Field(gt=0, le=1, description="Box height / image height.", examples=[0.06])


class FrameLabelsIn(BaseModel):
    """Boxes of a frame after a full-frame review (Label Studio). An empty list = reviewed background."""
    boxes:    list[ReviewBoxIn] = Field(description="Every object of the type on the frame.")
    reviewer: Optional[str] = Field(None, description="Who reviewed (default: the caller).",
                                    examples=["annotator@viasafe.cm"])


class TrainingFrameOut(BaseModel):
    """A detector frame of the training dataset."""
    model_config = ConfigDict(extra="allow")
    frame_key:      str = Field(description="Frame stem.")
    group_key:      str = Field(description="Leakage group (video + job + minute).")
    detection_type: str = Field(description="Detection type.")
    image_width:    Optional[int] = Field(None, description="Image width, pixels.")
    image_height:   Optional[int] = Field(None, description="Image height, pixels.")
    n_boxes:        int = Field(description="Boxes in the label.")
    status:         str = Field(description="`active`, `empty` (no active decision) or `no_image`.")
    review_status:  str = Field(description="`needs_box_review` or `box_reviewed`.")
    labels:         list[dict] = Field(description="Label boxes `{class_index, class_name, cx, cy, w, h, "
                                                   "detection_id}` (YOLO-normalised).")
    image_url:      Optional[str] = Field(None, description="Image download path (`/datasets/files/…`).")
    label_url:      Optional[str] = Field(None, description="Label download path.")


class TrainingSyncOut(BaseModel):
    """Result of a sync run."""
    frames: int = Field(description="Dirty frames processed.")
    synced: int = Field(description="Frames written.")
    failed: int = Field(description="Frames that failed (kept for a retry).")


@router.get("/training/summary", response_model=list[TrainingTypeSummaryOut], summary="Live training data per type")
async def training_summary(db: AsyncSession = Depends(get_db)):
    """
    Per detection type: decisions recorded (accepted / corrected / rejected, human vs automatic),
    detector frames (positive / background, boxes), classifier crops per class, box review state
    (Label Studio) and sync state of the objects in the `datasets` bucket. See docs/TRAINING-DATA.md.
    """
    return await td_export.summary(db)


@router.post("/training/sync", response_model=TrainingSyncOut, summary="Write pending training data now")
async def training_sync_now(db: AsyncSession = Depends(get_db)):
    """
    Writes the training-data objects of every pending decision (normally done in the background
    after each decision and retried by the beat task every 2 minutes). Failed frames are retried.
    """
    await db.execute(update(TrainingFrame).where(TrainingFrame.dirty.is_(True)).values(sync_attempts=0))
    await db.commit()
    return await training_sync.sync_dirty(db, limit=2000)


@router.get("/samples", response_model=TrainingSampleListOut, summary="List training samples",
            responses=errors(E_BAD_TYPE))
async def list_samples(
    detection_type: Optional[str] = Query(None, description="`pothole`, `speed_bump`, `traffic_sign` or "
                                                            "`parked_vehicle`.", examples=["pothole"]),
    decision: Optional[str] = Query(None, description="`accepted`, `corrected` or `rejected`."),
    review_status: Optional[str] = Query(None, description="Box review of the frame: `needs_box_review` or "
                                                           "`box_reviewed`."),
    state: str = Query("active", description="`active`, `superseded` or `all`."),
    decided_by: Optional[str] = Query(None, description="`human` or `system`."),
    since: Optional[datetime] = Query(None, description="Only samples changed at or after this time (UTC)."),
    skip: int = Query(0, ge=0, description="Number of items to skip (offset)."),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of items."),
    db: AsyncSession = Depends(get_db),
):
    """
    The live training samples — one per decided detection (id = detection id), newest change first,
    with the box review state of their frame. Reopened detections are `superseded` (no longer in
    the dataset).
    """
    q = select(TrainingSample, TrainingFrame.review_status).outerjoin(TrainingFrame, and_(
        TrainingFrame.detection_type == TrainingSample.detection_type,
        TrainingFrame.frame_key == TrainingSample.frame_key))
    if detection_type:
        q = q.where(TrainingSample.detection_type == _type(detection_type))
    if decision:
        q = q.where(TrainingSample.decision == decision)
    if review_status:
        q = q.where(TrainingFrame.review_status == review_status)
    if state != "all":
        q = q.where(TrainingSample.state == state)
    if decided_by:
        q = q.where(TrainingSample.decided_by == decided_by)
    if since:
        q = q.where(TrainingSample.updated_at >= since)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(TrainingSample.updated_at.desc()).offset(skip).limit(limit))).all()
    return {"total": total, "items": [td_export.sample_dict(s, rs) for s, rs in rows]}


@router.get("/export", summary="Export the training data of a type",
            responses={**errors(E_BAD_TYPE), 200: {"description": "`format=yolo`: a zip (layout in the "
                                                                  "description); `format=manifest`: JSON.",
                                                   "content": {"application/zip": {}, "application/json": {}}}})
async def export_training(
    request: Request,
    detection_type: str = Query(..., alias="type", description="Detection type.", examples=["pothole"]),
    format: str = Query("yolo", pattern="^(yolo|manifest)$", description="`yolo` (zip) or `manifest` (JSON)."),
    review_status: Optional[str] = Query(None, description="Only frames `needs_box_review` / `box_reviewed`."),
    decided_by: Optional[str] = Query(None, description="Only `human` or `system` decisions (default: both)."),
    since: Optional[datetime] = Query(None, description="Only frames / samples changed at or after this time."),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    The live training data of one detection type.

    * `format=manifest` — JSON `{detection_type, classes, layout, frames[], samples[]}`; every frame has
      `image_url` / `label_url` and its `labels` (YOLO-normalised boxes), every sample its `crop_url`
      — paths under `/datasets/files/` to download with the same credentials (the kit's
      `platform-pull` uses it).
    * `format=yolo` — a zip: `<type>/detector/{images,labels}/<frame_key>.jpg|txt`,
      `<type>/detector/classes.txt`, `data.yaml`, `<type>/classifier/<class>/<detection_id>.jpg`,
      `<type>/frames.csv`, `<type>/samples.csv`, `<type>/manifest.json`. Not split: split by
      `group_key` (video + job + minute) to avoid leakage.

    Audited as `dataset.training_export`.
    """
    det_type = _type(detection_type, required=True)
    man = await td_export.manifest(db, det_type, review_status=review_status, since=since,  # type: ignore[arg-type]
                                   decided_by=decided_by)
    await audit.record_now(request, "dataset.training_export", principal=principal, object_type="dataset",
                           object_id=det_type, details={"format": format, "frames": len(man["frames"]),
                                                        "samples": len(man["samples"])})
    if format == "manifest":
        return man
    tmp = await asyncio.to_thread(td_export.build_zip, man)

    def chunks():
        try:
            while True:
                b = tmp.read(1 << 20)
                if not b:
                    break
                yield b
        finally:
            tmp.close()
    name = f"viasafe-training-{det_type}-{datetime.utcnow():%Y%m%d-%H%M}.zip"
    return StreamingResponse(chunks(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/files/{key:path}", summary="Download a training-data file",
            responses={**errors(legacy(404, "File not found")),
                       200: {"description": "The object (JPEG image or text label).",
                             "content": {"image/jpeg": {}, "text/plain": {}}}})
async def get_training_file(key: Annotated[str, Path(description="Object key in the `datasets` bucket, e.g. "
                                                                 "`pothole/detector/images/<frame_key>.jpg`.")]):
    """One object of the `datasets` bucket (detector image / label, classifier crop, classes.txt)."""
    if ".." in key.split("/") or key.startswith("/"):
        raise HTTPException(404, "File not found")

    def read():
        from app.core.storage.minio_service import get_minio
        r = get_minio().get_object(settings.MINIO_BUCKET_DATASETS, key)
        try:
            return r.read(), r.headers.get("Content-Type") or "application/octet-stream"
        finally:
            r.close()
            r.release_conn()
    try:
        data, ctype = await asyncio.to_thread(read)
    except Exception:
        raise HTTPException(404, "File not found")
    return Response(content=data, media_type=ctype)


@router.put("/frames/{detection_type}/{frame_key}/labels", response_model=TrainingFrameOut,
            summary="Store the reviewed boxes of a frame",
            responses=errors(E_BAD_TYPE, E_NO_FRAME, E_BAD_BOXES))
async def put_frame_labels(
    detection_type: Annotated[str, Path(description="Detection type.", examples=["pothole"])],
    frame_key: Annotated[str, Path(description="Frame stem (`frame_key` of `GET /datasets/samples`).")],
    body: FrameLabelsIn, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal),
):
    """
    Box review write-back (kit `platform-push`, after Label Studio): the boxes replace the label the
    decisions produced for this frame (`review_status` → `box_reviewed`), the label file is rewritten.
    An empty list makes the frame a reviewed background. If a decision on the frame changes later,
    the frame goes back to `needs_box_review`. Audited as `dataset.frame_review`.
    """
    det_type = _type(detection_type, required=True)
    fr = (await db.execute(select(TrainingFrame).where(TrainingFrame.detection_type == det_type,
                                                       TrainingFrame.frame_key == frame_key))).scalar_one_or_none()
    if fr is None:
        raise api_error(404, "not_found", f"No training frame '{frame_key}' for {det_type}.")
    classes = card_info(det_type)[0]  # type: ignore[arg-type]
    try:
        boxes = td_export.clean_review_boxes([b.model_dump() for b in body.boxes], classes)
    except (ValueError, KeyError) as exc:
        raise api_error(422, "invalid_boxes", str(exc))
    td_export.apply_review(fr, boxes, body.reviewer or actor_name(principal) or principal.label or "anonymous")
    audit.record(db, request, "dataset.frame_review", principal=principal, object_type="training_frame",
                 object_id=f"{det_type}/{frame_key}", details={"boxes": len(boxes)})
    await db.commit()
    await training_sync.sync_dirty(db, frame_ids=[fr.id])
    await db.refresh(fr)
    return td_export.frame_dict(fr)


@router.get("/{dataset_id}", response_model=DatasetOut, summary="Get a dataset version",
            responses=errors(legacy(404, "Dataset not found")))
async def get_dataset(dataset_id: Annotated[str, Path(description="Dataset id (UUID).")],
                      db: AsyncSession = Depends(get_db)):
    """One dataset version with its class counts and file URLs."""
    ds = await db.get(Dataset, dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    return ds
