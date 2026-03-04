# Radio Stream Recorder Spotwise homework

A production-ready tool that continuously records live radio streams and saves audio as segmented MP3 files. Supports both HLS (`.m3u8`) and direct HTTP streams, handles reconnects automatically, and shuts down gracefully without corrupting the current file.

---

## What the tool does

- Connects to a live radio stream — either **HLS** (`.m3u8` playlist) or **direct HTTP** (AAC, MP3, etc.)
- Records audio continuously and splits it into **time-based MP3 chunks** (default: 10 minutes)
- Writes chunks to a local directory with clean, timestamped filenames
- **Auto-reconnects** if the stream drops, with exponential back-off
- On `Ctrl+C` or `docker stop`, **finalises the current segment cleanly** before exiting — no corrupted files
- Optionally **uploads completed segments to Azure Blob Storage** in the background

Under the hood, Python manages the process lifecycle and reconnect logic while `ffmpeg` handles all the stream parsing, codec handling, and segmentation.

---

## How to build and run locally with Docker Desktop

### Prerequisites

- [Docker](https://docs.docker.com/engine/install/) installed and running

### 1. Clone the repository

```bash
git clone https://github.com/AGWave01/spotwise-hw.git
cd spotwise-hw
```

### 2. Build the image

```bash
docker build -t radio-recorder .
```

### 3. Run — single station

```bash
# HLS stream
docker run --rm \
  -e STREAM_URL="https://stream.dimensionesuonoroma.radio/audio/dsr.stream_aac64/playlist.m3u8" \
  -e STATION_NAME="dsr" \
  -v "$(pwd)/recordings:/recordings" \
  radio-recorder
```

```bash
docker run --rm \
  -e STREAM_URL="https://stream3.ehrhiti.lv:8000/Stream_93_LV01.aac" \
  -e STATION_NAME="ehrhiti" \
  -v "$(pwd)/recordings:/recordings" \
  radio-recorder
```

MP3 files will appear in `./recordings/` within the first minute. Press **Ctrl+C** to stop — the current segment is finalised before exit.

### 4. Run — two stations in parallel

```bash
docker compose up --build
```

This starts both test streams simultaneously. Files are written to `./recordings/dsr/` and `./recordings/ehrhiti/`.

```bash
# Stop both
docker compose down
```

### 5. Verify output

```
recordings/
├── dsr/
│   ├── dsr_20260304_190557_0000.mp3
│   └── dsr_20260304_190557_0001.mp3
└── ehrhiti/
    └── ehrhiti_20260304_190600_0000.mp3
```

All files should be playable in any audio player.

---

## Environment variables / configuration options

| Variable                          | CLI flag           | Default       | Description                                              |
| --------------------------------- | ------------------ | ------------- | -------------------------------------------------------- |
| `STREAM_URL`                      | `--url`            | *required*  | Stream URL HLS `.m3u8` or direct HTTP                  |
| `OUTPUT_DIR`                      | `--output-dir`     | `/recordings` | Directory where MP3 segments are written                 |
| `CHUNK_DURATION`                  | `--chunk-duration` | `600`         | Segment length in seconds (600 = 10 min)                 |
| `STATION_NAME`                    | `--station-name`   | `station`     | Prefix used in output filenames                          |
| `LOG_LEVEL`                       | —                  | `INFO`        | `DEBUG` / `INFO` / `WARNING` / `ERROR`                   |
| `AZURE_STORAGE_CONNECTION_STRING` | —                  | *unset*     | If set, uploads completed segments to Azure Blob Storage |
| `AZURE_STORAGE_CONTAINER`         | —                  | `recordings`  | Azure Blob container name                                |

cli flags take precedence over environment variables.

**Output filename format:**
```
{STATION_NAME}_{YYYYMMDD_HHMMSS}_{seq:04d}.mp3

# Example:
dsr_20260304_190557_0000.mp3
dsr_20260304_190557_0001.mp3
```

The timestamp resets on each recorder restart — files are never overwritten.

---

## Running without Docker

Requires Python 3.10+ and `ffmpeg` installed on the system.

```bash
pip install -r requirements.txt

python recorder.py \
  --url "https://stream.dimensionesuonoroma.radio/audio/dsr.stream_aac64/playlist.m3u8" \
  --station-name dsr \
  --output-dir ./recordings \
  --chunk-duration 600
```

---

## Azure deployment

Two deployment plans are included in this repository:

| File                                                             | Description                                                                                                                      |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| [`az-deployment-standart-v.md`](./az-deployment-standart-v.md) | Managed services: Container Apps, Key Vault, Event Grid, Service Bus, Log Analytics — ~$55/month for 20 stations                 |
| [`az-deployment-prem.approach-v.md`](./az-deployment-prem.approach-v.md)     | Single VM + docker compose, self-hosted Vault + VictoriaMetrics + Grafana — ~$75/month for 20 stations, lower Azure managed |

Both plans include architecture diagrams, resource justification, scaling strategy with Terraform, and deployment steps via Azure cli and Terraform.