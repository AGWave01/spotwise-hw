FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

LABEL maintainer="radio-recorder"
LABEL description="Continuous radio stream recorder HLS & HTTP -> segmented MP3"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app
COPY recorder.py .

RUN mkdir -p /recordings

RUN groupadd -r recorder && useradd -r -g recorder recorder \
    && chown recorder:recorder /recordings /app

USER recorder

ENV STREAM_URL=""
ENV OUTPUT_DIR="/recordings"
ENV CHUNK_DURATION="600"
ENV STATION_NAME="station"
ENV LOG_LEVEL="INFO"

VOLUME ["/recordings"]

ENTRYPOINT ["python", "recorder.py"]