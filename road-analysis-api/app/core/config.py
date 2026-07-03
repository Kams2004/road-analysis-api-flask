from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/road_analysis"

    # MinIO
    MINIO_ENDPOINT:          str  = "localhost:9000"
    MINIO_ACCESS_KEY:        str  = "minioadmin"
    MINIO_SECRET_KEY:        str  = "minioadmin"
    MINIO_BUCKET_DETECTIONS:    str  = "detections"
    MINIO_BUCKET_DATASETS:      str  = "datasets"
    MINIO_BUCKET_SIGNALEMENTS:  str  = "signalements"
    MINIO_SECURE:            bool = False

    # Models paths
    MODEL_POTHOLE:     str = "/models/model_pothole.pt"
    MODEL_SIGNS:       str = "/models/model_signs.pt"
    MODEL_SPEEDBUMP:   str = "/models/model_speedbump.pt"
    MODEL_CLASSIFIER:  str = "/models/model_pothole_classifier.pt"

    # Processing
    TEMP_DIR:            str   = "/tmp/road_analysis"
    FRAME_SKIP:          int   = 5
    CONF_THRESHOLD:      float = 0.25
    IOU_THRESHOLD:       float = 0.4
    DEDUP_DISTANCE_PX:   int   = 50

    # Celery / Redis
    CELERY_BROKER_URL:      str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND:  str = "redis://localhost:6379/1"

    # Clustering
    CLUSTER_RADIUS_M: float = 50.0   # default cluster radius in metres

    # Watchdog
    STALE_JOB_MINUTES: int = 60

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
