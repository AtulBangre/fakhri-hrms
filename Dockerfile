# syntax=docker/dockerfile:1
#
# Fakhri HRMS — portable image. Works on any x86-64 or arm64 host with Docker.
#
#   docker build -t fakhri-hrms .
#   docker run -p 8100:8100 -v hrms-data:/app/data \
#     -e HRMS_FACEAPI_URL=http://host.docker.internal:8000 fakhri-hrms
#
# Note: SQLite + in-process sessions mean this image is designed to run as a
# single instance (or one instance per site). Scale up, not out, unless you move
# state to Postgres.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    HRMS_DATA_DIR=/app/data \
    PORT=8100 \
    WORKERS=1

# Fonts are only needed if you regenerate the PWA icons inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10002 --shell /usr/sbin/nologin hrms

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app ./app
COPY scripts ./scripts
COPY cli.py ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

RUN mkdir -p /app/data && chown -R hrms:hrms /app

USER hrms
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8100'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["./docker-entrypoint.sh"]
