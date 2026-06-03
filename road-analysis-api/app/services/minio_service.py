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
        for bucket in [settings.MINIO_BUCKET_DETECTIONS]:
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
