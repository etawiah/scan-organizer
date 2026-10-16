# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCANS_ROOT=/scans

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY scan_organizer.py /app/scan_organizer.py
COPY scanorganizer /app/scanorganizer
COPY taxonomy.yaml /app/taxonomy.yaml
COPY private_terms.example.txt /app/private_terms.example.txt

# Allow runtime config overrides via /config bind mount.
# At container start we overlay /config over /app for taxonomy.yaml and
# private_terms.txt without copying anything the user did not provide.
RUN install -d /scans /config

ENTRYPOINT ["python3", "/app/scan_organizer.py"]
CMD ["--non-interactive"]
