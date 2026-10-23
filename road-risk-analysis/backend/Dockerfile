# Build context: backend/ (from infra/: `build: ../backend`). Weights and secrets are
# mounted at run time (ai/models -> /models, infra/secrets -> /app/secrets).
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 libgthread-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --timeout=600 --retries=10 \
    torch==2.3.1+cpu torchvision==0.18.1+cpu \
    --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    fastapi "uvicorn[standard]" pydantic-settings python-multipart && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    "sqlalchemy[asyncio]" asyncpg alembic && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    minio "celery[redis]" redis && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    ultralytics==8.3.40 opencv-python-headless numpy setuptools && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    paddlepaddle==2.6.2 paddleocr==2.8.1

# Kept as its own layer so adding it doesn't rebuild the heavy ML layer above.
RUN pip install --no-cache-dir --timeout=600 --retries=10 "cryptography>=42.0.0"

# SFTP video sources — again its own small layer: the torch / ML layers above stay cached.
RUN pip install --no-cache-dir --timeout=600 --retries=10 "paramiko==4.0.0"

# PDF reports (Module 14) — own small layer so the ML layers above stay cached.
RUN pip install --no-cache-dir --timeout=600 --retries=10 "reportlab==4.4.4"

# ffmpeg: H.264 context clips (browser-playable) — its own small layer, after the ML layers.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# PaddleOCR downloads its models on first use into /root/.paddleocr: the server stack keeps that
# folder in a volume (ocr_models), so they are fetched once, not at every redeploy. (Initialising
# PaddleOCR during `docker build` segfaults — zlib clash inside the build environment.)
COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
