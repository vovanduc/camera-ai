FROM python:3.12-slim
# ⚠️ Module OFF mặc định (build-and-shelve). Stack OSNet/InsightFace = non-commercial:
#    KHÔNG enable cho khách trả tiền tới khi swap permissive (xem spec phase2 §9 OQ1).
# ⚠️ Image nặng (~3-4GB torch). Build defer x86 (arm64 torch build blocked — Phase 0).

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    INSIGHTFACE_HOME=/app/data/.insightface

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 libgl1 libglib2.0-0 \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src /app/src

CMD ["python", "-m", "reid_worker.main"]
