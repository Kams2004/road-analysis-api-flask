FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 libgthread-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY wheels/ ./wheels/
RUN pip install --no-cache-dir wheels/torch-2.3.1+cpu-cp312-cp312-linux_x86_64.whl wheels/torchvision-0.18.1+cpu-cp312-cp312-linux_x86_64.whl

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout=600 --retries=10 \
    fastapi "uvicorn[standard]" pydantic-settings python-multipart && \
    sleep 5 && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    "sqlalchemy[asyncio]" asyncpg alembic && \
    sleep 5 && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    minio "celery[redis]" redis && \
    sleep 5 && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    ultralytics==8.3.40 opencv-python-headless numpy setuptools && \
    sleep 5 && \
    pip install --no-cache-dir --timeout=600 --retries=10 \
    paddlepaddle==2.6.2 paddleocr==2.8.1

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
