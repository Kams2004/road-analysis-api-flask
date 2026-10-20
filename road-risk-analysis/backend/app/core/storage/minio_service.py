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


def upload_image(data: bytes, object_name: str) -> str:
    """Upload JPEG bytes to MinIO, return public URL path."""
    client = get_minio()
    client.put_object(
        settings.MINIO_BUCKET_DETECTIONS,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type="image/jpeg",
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
