#!/usr/bin/env python3

import os
import sys
import time
import signal
import logging
import argparse
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    from azure.storage.blob import BlobServiceClient
    AZURE_AVAILABLE = True
except ImportError:
    AZURE_AVAILABLE = False

def setup_logging(level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    return logging.getLogger("recorder")


logger = setup_logging(os.environ.get("LOG_LEVEL", "INFO"))


class AzureUploader:

    def __init__(self, connection_string: str, container_name: str, station_name: str):
        self.container_name = container_name
        self.station_name = station_name
        self._client = BlobServiceClient.from_connection_string(connection_string)
        self._uploaded: set = set()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _is_stable(self, path: Path, wait: float = 3.0) -> bool:
        try:
            size_before = path.stat().st_size
            time.sleep(wait)
            size_after = path.stat().st_size
            return size_before == size_after and size_after > 0
        except OSError:
            return False

    def _upload(self, path: Path):
        blob_name = f"{self.station_name}/{path.name}"
        try:
            container = self._client.get_container_client(self.container_name)
            with open(path, "rb") as data:
                container.upload_blob(name=blob_name, data=data, overwrite=True)
            logger.info(f"[azure] uploaded → {blob_name}")
        except Exception as exc:
            logger.error(f"[azure] upload failed for {path.name}: {exc}")

    def watch(self, directory: Path):
        logger.info(f"[azure] watcher started → container={self.container_name}")
        while not self._stop_event.is_set():
            for mp3 in sorted(directory.glob("*.mp3")):
                if mp3 not in self._uploaded and self._is_stable(mp3):
                    self._upload(mp3)
                    self._uploaded.add(mp3)
            self._stop_event.wait(timeout=10)
        logger.info("[azure] watcher stopped")

class RadioRecorder:
    def __init__(
        self,
        stream_url: str,
        output_dir: Path,
        chunk_duration: int,
        station_name: str,
        azure_connection: str | None = None,
        azure_container: str = "recordings",
    ):
        self.stream_url = stream_url
        self.output_dir = output_dir
        self.chunk_duration = chunk_duration
        self.station_name = station_name
        self.azure_connection = azure_connection
        self.azure_container = azure_container

        self._running = False
        self._process: subprocess.Popen | None = None
        self._uploader: AzureUploader | None = None
        self._uploader_thread: threading.Thread | None = None

        self.output_dir.mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum, _frame):
        logger.info(f"Signal {signum} received — stopping gracefully …")
        self._running = False
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _segment_pattern(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return str(self.output_dir / f"{self.station_name}_{ts}_%04d.mp3")

    def _build_cmd(self, pattern: str) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "30",
            "-i", self.stream_url,
            "-vn",
            "-c:a", "libmp3lame",
            "-q:a", "4",
            "-ar", "44100",
            "-ac", "2",
            "-f", "segment",
            "-segment_time", str(self.chunk_duration),
            "-segment_format", "mp3",
            "-reset_timestamps", "1",
            "-strftime", "0",
            pattern,
        ]

    def _start_uploader(self):
        if not self.azure_connection or not AZURE_AVAILABLE:
            if self.azure_connection and not AZURE_AVAILABLE:
                logger.warning("azure-storage-blob not installed — upload disabled")
            return
        self._uploader = AzureUploader(
            self.azure_connection, self.azure_container, self.station_name
        )
        self._uploader_thread = threading.Thread(
            target=self._uploader.watch,
            args=(self.output_dir,),
            daemon=True,
        )
        self._uploader_thread.start()

    def _stop_uploader(self):
        if self._uploader:
            self._uploader.stop()


    def record(self):
        self._running = True
        attempt = 0
        base_delay = 5

        logger.info(f"Stream URL    : {self.stream_url}")
        logger.info(f"Output dir    : {self.output_dir}")
        logger.info(f"Chunk duration: {self.chunk_duration}s")
        logger.info(f"Station name  : {self.station_name}")
        if AZURE_AVAILABLE and self.azure_connection:
            logger.info(f"Azure container: {self.azure_container}")

        self._start_uploader()

        while self._running:
            pattern = self._segment_pattern()
            cmd = self._build_cmd(pattern)
            attempt += 1
            logger.info(f"[ffmpeg] starting (attempt #{attempt}) …")
            logger.debug("CMD: " + " ".join(cmd))

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                _, stderr_bytes = self._process.communicate()
                rc = self._process.returncode

                if not self._running:
                    break

                stderr_tail = (stderr_bytes or b"").decode("utf-8", errors="replace")[-600:]
                if rc == 0:
                    logger.info("[ffmpeg] exited cleanly — restarting …")
                    attempt = 0
                else:
                    logger.warning(f"[ffmpeg] exited with code {rc}")
                    if stderr_tail:
                        logger.warning(f"[ffmpeg] stderr: {stderr_tail}")

            except FileNotFoundError:
                logger.critical("ffmpeg not found — is it installed and in PATH?")
                sys.exit(1)
            except Exception as exc:
                logger.error(f"Unexpected error: {exc}")

            if self._running:
                delay = min(base_delay * attempt, 60)
                logger.info(f"Reconnecting in {delay}s …")
                time.sleep(delay)

        self._stop_uploader()
        logger.info("Recorder stopped.")

def parse_config() -> dict:
    parser = argparse.ArgumentParser(
        description="Record a live radio stream to segmented mp3 files",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("STREAM_URL"),
        help="Stream url (hls .m3u8 or direct http) env: STREAM_URL",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("OUTPUT_DIR", "/recordings"),
        help="Directory to write mp3 segments env: OUTPUT_DIR",
    )
    parser.add_argument(
        "--chunk-duration",
        type=int,
        default=int(os.environ.get("CHUNK_DURATION", "600")),
        help="Segment length in seconds. env: CHUNK_DURATION",
    )
    parser.add_argument(
        "--station-name",
        default=os.environ.get("STATION_NAME", "station"),
        help="Label used in output filenames. env: STATION_NAME",
    )
    args = parser.parse_args()

    if not args.url:
        parser.error("Stream url is required: --url or STREAM_URL env var")

    return {
        "stream_url": args.url,
        "output_dir": Path(args.output_dir),
        "chunk_duration": args.chunk_duration,
        "station_name": args.station_name,
        "azure_connection": os.environ.get("AZURE_STORAGE_CONNECTION_STRING"),
        "azure_container": os.environ.get("AZURE_STORAGE_CONTAINER", "recordings"),
    }


def main():
    cfg = parse_config()
    recorder = RadioRecorder(**cfg)
    recorder.record()


if __name__ == "__main__":
    main()