"""
Dataset export service.

Creates an immutable versioned snapshot of all validated labels for a
detection type. Each version:
  - copies crop images into datasets/{type}/v{N}/crops/{label}/
  - writes manifest.json  (list of all sample records)
  - writes metadata.json  (version info, class distribution, timestamps)
  - inserts Dataset + DatasetSample rows in PostgreSQL
"""
import io
import json
import logging
from datetime import datetime
from collections import Counter

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.dataset import Dataset, DatasetSample
from app.models.validation_label import ValidationLabel
from app.services.minio_service import get_minio

logger = logging.getLogger(__name__)


def _copy_crop(src_object: str, dst_object: str) -> str | None:
    """Copy a crop within MinIO from detections bucket to datasets bucket."""
    client = get_minio()
    try:
        from minio.commonconfig import CopySource
        client.copy_object(
            settings.MINIO_BUCKET_DATASETS,
            dst_object,
            CopySource(settings.MINIO_BUCKET_DETECTIONS, src_object),
        )
        return f"{settings.MINIO_ENDPOINT}/{settings.MINIO_BUCKET_DATASETS}/{dst_object}"
    except Exception as e:
        logger.warning(f"Crop copy failed {src_object} → {dst_object}: {e}")
        return None


def _upload_json(data: dict, object_name: str) -> str:
    client = get_minio()
    body = json.dumps(data, indent=2, default=str).encode()
    client.put_object(
        settings.MINIO_BUCKET_DATASETS,
        object_name,
        io.BytesIO(body),
        length=len(body),
        content_type="application/json",
    )
    return f"{settings.MINIO_ENDPOINT}/{settings.MINIO_BUCKET_DATASETS}/{object_name}"


async def export_dataset(
    detection_type: str,
    created_by: str,
    db: AsyncSession,
) -> Dataset:
    """
    Snapshot all validated labels for detection_type into a new version.
    Returns the created Dataset row.
    """
    # Ensure datasets bucket exists
    client = get_minio()
    if not client.bucket_exists(settings.MINIO_BUCKET_DATASETS):
        client.make_bucket(settings.MINIO_BUCKET_DATASETS)

    # Determine next version number for this detection_type
    max_ver = (await db.execute(
        select(func.max(Dataset.version)).where(Dataset.detection_type == detection_type)
    )).scalar() or 0
    version = max_ver + 1

    base_path = f"{detection_type}/v{version}"

    # Load all validated labels for this type
    rows: list[ValidationLabel] = (await db.execute(
        select(ValidationLabel).where(ValidationLabel.detection_type == detection_type)
    )).scalars().all()

    if not rows:
        raise ValueError(f"No validated labels found for detection_type='{detection_type}'")

    class_counts: Counter = Counter()
    manifest_entries = []
    samples = []

    for vl in rows:
        class_counts[vl.label] += 1

        # Copy crop to frozen location: datasets/{type}/v{N}/crops/{label}/{id}.jpg
        frozen_crop_url = None
        if vl.crop_url:
            # Extract the object name from the full URL
            # URL format: {endpoint}/{bucket}/{object_name}
            prefix = f"{settings.MINIO_ENDPOINT}/{settings.MINIO_BUCKET_DETECTIONS}/"
            if vl.crop_url.startswith(prefix):
                src_object = vl.crop_url[len(prefix):]
            else:
                src_object = vl.crop_url
            dst_object = f"{base_path}/crops/{vl.label}/{vl.id}.jpg"
            frozen_crop_url = _copy_crop(src_object, dst_object)

        manifest_entries.append({
            "validation_label_id": vl.id,
            "detection_id":        vl.detection_id,
            "label":               vl.label,
            "severity_score":      vl.severity_score,
            "model_confidence":    vl.model_confidence,
            "crop_url":            frozen_crop_url,
            "labeled_by":          vl.labeled_by,
            "labeled_at":          vl.labeled_at.isoformat() if vl.labeled_at else None,
        })

        samples.append(DatasetSample(
            validation_label_id=vl.id,
            label=vl.label,
            severity_score=vl.severity_score,
            model_confidence=vl.model_confidence,
            crop_url=frozen_crop_url,
        ))

    # Write manifest.json
    manifest_url = _upload_json(
        {"version": version, "detection_type": detection_type, "samples": manifest_entries},
        f"{base_path}/manifest.json",
    )

    # Write metadata.json
    metadata_url = _upload_json(
        {
            "detection_type":  detection_type,
            "version":         version,
            "total_samples":   len(rows),
            "class_counts":    dict(class_counts),
            "created_by":      created_by,
            "created_at":      datetime.utcnow().isoformat(),
        },
        f"{base_path}/metadata.json",
    )

    # Persist Dataset row
    dataset = Dataset(
        detection_type=detection_type,
        version=version,
        total_samples=len(rows),
        class_counts=dict(class_counts),
        manifest_url=manifest_url,
        metadata_url=metadata_url,
        created_by=created_by,
    )
    db.add(dataset)
    await db.flush()  # get dataset.id before adding samples

    for s in samples:
        s.dataset_id = dataset.id
        db.add(s)

    await db.commit()
    await db.refresh(dataset)
    logger.info(
        f"Dataset exported: type={detection_type} v{version} "
        f"samples={len(rows)} classes={dict(class_counts)}"
    )
    return dataset
