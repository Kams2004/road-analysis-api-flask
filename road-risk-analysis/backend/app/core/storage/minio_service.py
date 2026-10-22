import io
from minio import Minio
from minio.error import S3Error
from app.core.config import settings

_client: Minio = None


def get_minio() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
        for bucket in [settings.MINIO_BUCKET_DETECTIONS, settings.MINIO_BUCKET_SIGNALEMENTS]:
            if not _client.bucket_exists(bucket):
                _client.make_bucket(bucket)
    return _client


def upload_image(data: bytes, object_name: str, content_type: str = "image/jpeg") -> str:
    """Upload bytes (JPEG by default; context clips pass their video type) to the
    detections bucket, return public URL path."""
    client = get_minio()
    client.put_object(
        settings.MINIO_BUCKET_DETECTIONS,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return f"{settings.MINIO_ENDPOINT}/{settings.MINIO_BUCKET_DETECTIONS}/{object_name}"


def upload_signalement_image(data: bytes, object_name: str, content_type: str = "image/jpeg") -> str:
    """Upload a signalement image to MinIO, return public URL path."""
    client = get_minio()
    client.put_object(
        settings.MINIO_BUCKET_SIGNALEMENTS,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return f"{settings.MINIO_ENDPOINT}/{settings.MINIO_BUCKET_SIGNALEMENTS}/{object_name}"


def upload_signalement_audio(data: bytes, object_name: str, content_type: str = "audio/m4a") -> str:
    """Upload a signalement audio recording to MinIO, return public URL path."""
    client = get_minio()
    client.put_object(
        settings.MINIO_BUCKET_SIGNALEMENTS,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return f"{settings.MINIO_ENDPOINT}/{settings.MINIO_BUCKET_SIGNALEMENTS}/{object_name}"


# ── Generated reports (Module 14) — private bucket, served only through the
# authenticated GET /reports/{id}/download. ─────────────────────────────────

def _ensure_bucket(client: Minio, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def upload_report(data: bytes, object_name: str) -> None:
    client = get_minio()
    _ensure_bucket(client, settings.MINIO_BUCKET_REPORTS)
    client.put_object(settings.MINIO_BUCKET_REPORTS, object_name, io.BytesIO(data),
                      length=len(data), content_type="application/pdf")


def get_report(object_name: str) -> bytes:
    resp = get_minio().get_object(settings.MINIO_BUCKET_REPORTS, object_name)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def remove_report(object_name: str) -> None:
    try:
        get_minio().remove_object(settings.MINIO_BUCKET_REPORTS, object_name)
    except S3Error:
        pass


def read_object_url(url: str, max_bytes: int = 15_000_000) -> bytes | None:
    """Bytes of an object stored as "<endpoint>/<bucket>/<object>" (the URL
    form saved on detections / reports), or None when it cannot be read."""
    path = url.split("://", 1)[-1]
    parts = path.split("/", 2)
    if len(parts) < 3:
        return None
    _, bucket, obj = parts
    try:
        resp = get_minio().get_object(bucket, obj)
        try:
            return resp.read(max_bytes)
        finally:
            resp.close()
            resp.release_conn()
    except Exception:
        return None
