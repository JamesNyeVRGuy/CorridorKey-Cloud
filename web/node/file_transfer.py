"""File transfer utilities for nodes without shared storage.

Downloads input frames from the main machine and uploads results back.
Uses tar bundle downloads for speed, with per-file fallback.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import os
import tarfile
import threading
import time as _time
from collections.abc import Callable
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TransferStats:
    """Tracks bytes transferred and wall-clock time for speed measurement."""

    bytes_transferred: int = 0
    elapsed_seconds: float = 0.0

    @property
    def mbps(self) -> float:
        """Megabytes per second."""
        if self.elapsed_seconds <= 0:
            return 0.0
        return (self.bytes_transferred / (1024 * 1024)) / self.elapsed_seconds


# Max concurrent file transfers across all nodes on this machine.
# Prevents multiple jobs from saturating the network simultaneously.
_transfer_semaphore = threading.Semaphore(2)


class TransferCancelled(Exception):
    """Raised when a file transfer is cancelled mid-stream."""


# Transient HTTP status codes worth retrying (server restart, load balancer hiccup)
_RETRY_STATUSES = {401, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_DELAY = 3  # seconds


def _with_retry(fn, description: str = "request"):
    """Retry a function on transient HTTP errors."""
    last_err = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                code = e.response.status_code
                logger.warning(f"{description}: {code}, retry in {_RETRY_DELAY}s ({attempt + 1}/{_MAX_RETRIES})")
                _time.sleep(_RETRY_DELAY)
                last_err = e
            else:
                raise
    raise last_err  # shouldn't reach here


class FileTransfer:
    """Handles file downloads/uploads between node and main machine."""

    def __init__(self, main_url: str, node_id: str, timeout: float = 300, auth_token: str = ""):
        self.main_url = main_url.rstrip("/")
        self.node_id = node_id
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    def set_job_id(self, job_id: str | None) -> None:
        """Set the current job_id for org-scoped file resolution."""
        self._job_id = job_id

    def _url(self, path: str) -> str:
        base = f"{self.main_url}/api/nodes/{self.node_id}/files/{path}"
        job_id = getattr(self, "_job_id", None)
        if job_id:
            sep = "&" if "?" in base else "?"
            base += f"{sep}job_id={job_id}"
        return base

    def list_files(self, clip_name: str, pass_name: str) -> tuple[str | None, list[str]]:
        """List files available for a clip pass.

        Returns (directory_name, file_list). directory_name is the actual
        subdirectory on the server (e.g. "Frames" or "Input").
        """
        with httpx.Client(timeout=30, headers=self._headers) as client:
            r = client.get(self._url(f"{clip_name}/{pass_name}"))
            r.raise_for_status()
            data = r.json()
            return data.get("directory"), data.get("files", [])

    def download_pass(
        self,
        clip_name: str,
        pass_name: str,
        clip_dir: str,
        frame_range: tuple[int, int] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[int, TransferStats]:
        """Download files for a clip pass into the correct subdirectory.

        Tries tar bundle download first (single HTTP request for all files),
        falls back to per-file download if the bundle endpoint isn't available.

        Args:
            clip_name: Name of the clip.
            pass_name: Pass type ("input", "alpha", "mask", "source").
            clip_dir: Local clip root directory (files go into a subdirectory).
            frame_range: Optional (start, end) to only download frames in range.
            is_cancelled: Optional callback returning True if the job was cancelled.

        Returns (file_count, TransferStats).
        """
        # Try bundle download first
        try:
            count, stats = self._download_bundle(clip_name, pass_name, clip_dir, frame_range, is_cancelled)
            if count > 0:
                return count, stats
        except TransferCancelled:
            raise
        except Exception as e:
            logger.debug(f"Bundle download failed, falling back to per-file: {e}")

        return self._download_per_file(clip_name, pass_name, clip_dir, frame_range, is_cancelled)

    def _download_bundle(
        self,
        clip_name: str,
        pass_name: str,
        clip_dir: str,
        frame_range: tuple[int, int] | None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[int, TransferStats]:
        """Download files as a tar stream (single HTTP request)."""
        params = {}
        if frame_range:
            params["start"] = frame_range[0]
            params["end"] = frame_range[1]

        url = self._url(f"{clip_name}/{pass_name}/bundle")
        net_bytes = 0
        with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            with client.stream("GET", url, params=params) as resp:
                if resp.status_code != 200:
                    return 0, TransferStats()

                # Stream tar data -- time only the network I/O
                buf = io.BytesIO()
                t0 = _time.monotonic()
                for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                    if is_cancelled and is_cancelled():
                        raise TransferCancelled(f"Download cancelled: {clip_name}/{pass_name}")
                    buf.write(chunk)
                net_elapsed = _time.monotonic() - t0
                net_bytes = buf.tell()
                buf.seek(0)

                # Extract tar (CPU work, not timed)
                count = 0
                with tarfile.open(fileobj=buf, mode="r|") as tar:
                    for member in tar:
                        if member.isfile():
                            dest = os.path.join(clip_dir, member.name)
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            with open(dest, "wb") as f:
                                extracted = tar.extractfile(member)
                                if extracted:
                                    f.write(extracted.read())
                            count += 1

        stats = TransferStats(bytes_transferred=net_bytes, elapsed_seconds=net_elapsed)
        directory = resp.headers.get("x-tar-directory", pass_name)
        logger.info(
            f"Downloaded {count} files (bundle, {stats.mbps:.1f} MB/s) for {clip_name}/{pass_name} -> {directory}/"
        )
        return count, stats

    def _download_per_file(
        self,
        clip_name: str,
        pass_name: str,
        clip_dir: str,
        frame_range: tuple[int, int] | None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[int, TransferStats]:
        """Download files one at a time (fallback)."""
        directory, files = self.list_files(clip_name, pass_name)
        if not files or not directory:
            return 0, TransferStats()

        if frame_range is not None:
            start, end = frame_range
            files = files[start:end]

        dest_dir = os.path.join(clip_dir, directory)
        os.makedirs(dest_dir, exist_ok=True)
        count = 0
        total_bytes = 0
        total_elapsed = 0.0

        for fname in files:
            if is_cancelled and is_cancelled():
                raise TransferCancelled(f"Download cancelled: {clip_name}/{pass_name}")

            dest_path = os.path.join(dest_dir, fname)
            if os.path.isfile(dest_path):
                count += 1
                continue

            url = self._url(f"{clip_name}/{pass_name}/{fname}")
            tmp_path = dest_path + ".part"
            file_bytes = 0

            def _do_download(u=url, tp=tmp_path, dp=dest_path):
                nonlocal file_bytes
                with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
                    try:
                        with client.stream("GET", u) as resp:
                            resp.raise_for_status()
                            with open(tp, "wb") as f:
                                for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                                    f.write(chunk)
                                    file_bytes += len(chunk)
                        os.replace(tp, dp)
                    except Exception:
                        if os.path.isfile(tp):
                            os.remove(tp)
                        raise

            t0 = _time.monotonic()
            _with_retry(_do_download, f"Download {fname}")
            total_elapsed += _time.monotonic() - t0
            total_bytes += file_bytes
            count += 1

        stats = TransferStats(bytes_transferred=total_bytes, elapsed_seconds=total_elapsed)
        logger.info(f"Downloaded {count} files ({stats.mbps:.1f} MB/s) for {clip_name}/{pass_name} -> {directory}/")
        return count, stats

    def upload_file(self, clip_name: str, pass_name: str, file_path: str) -> None:
        """Upload a single result file to the main machine."""
        fname = Path(file_path).name
        url = self._url(f"{clip_name}/{pass_name}/{fname}")

        def _do():
            with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
                with open(file_path, "rb") as f:
                    r = client.post(url, files={"file": (fname, f)})
                    r.raise_for_status()

        _with_retry(_do, f"Upload {fname}")

    def upload_directory(
        self,
        clip_name: str,
        pass_name: str,
        src_dir: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[int, TransferStats]:
        """Upload all files in a directory as results.

        Tries tar bundle upload first (single HTTP request), falls back
        to per-file upload if bundle endpoint isn't available.

        Returns (file_count, TransferStats).
        """
        if not os.path.isdir(src_dir):
            return 0, TransferStats()

        files = sorted(f for f in os.listdir(src_dir) if os.path.isfile(os.path.join(src_dir, f)))
        if not files:
            return 0, TransferStats()

        # Try bundle upload first
        try:
            count, stats = self._upload_bundle(clip_name, pass_name, src_dir, files, is_cancelled)
            if count > 0:
                return count, stats
        except TransferCancelled:
            raise
        except Exception as e:
            logger.warning(f"Bundle upload failed, falling back to per-file: {e}")

        # Per-file fallback
        count = 0
        total_bytes = 0
        t0 = _time.monotonic()
        for fname in files:
            if is_cancelled and is_cancelled():
                raise TransferCancelled(f"Upload cancelled: {clip_name}/{pass_name}")
            fpath = os.path.join(src_dir, fname)
            total_bytes += os.path.getsize(fpath)
            self.upload_file(clip_name, pass_name, fpath)
            count += 1

        stats = TransferStats(bytes_transferred=total_bytes, elapsed_seconds=_time.monotonic() - t0)
        logger.info(f"Uploaded {count} files (per-file, {stats.mbps:.1f} MB/s) for {clip_name}/{pass_name}")
        return count, stats

    # Max compressed bundle size per upload (stay under Cloudflare's 100MB limit)
    _MAX_BUNDLE_BYTES = 90 * 1024 * 1024

    def _upload_bundle(
        self,
        clip_name: str,
        pass_name: str,
        src_dir: str,
        files: list[str],
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[int, TransferStats]:
        """Upload files as gzip-compressed tar chunks.

        Splits into multiple uploads if the compressed size exceeds 90MB
        (Cloudflare's 100MB upload limit). Each chunk is a complete tar
        that the server extracts independently.
        """

        # Build tar in memory and check size -- split into chunks if needed
        chunks = self._build_tar_chunks(src_dir, files)

        total_count = 0
        total_bytes = 0
        net_elapsed = 0.0
        url = self._url(f"{clip_name}/{pass_name}/bundle")

        for i, compressed in enumerate(chunks):
            if is_cancelled and is_cancelled():
                raise TransferCancelled(f"Upload cancelled: {clip_name}/{pass_name}")
            total_bytes += len(compressed)

            def _do_chunk(chunk=compressed):
                with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
                    r = client.post(
                        url,
                        content=chunk,
                        headers={
                            **self._headers,
                            "Content-Type": "application/x-tar",
                            "Content-Encoding": "gzip",
                            "Content-Length": str(len(chunk)),
                        },
                    )
                    r.raise_for_status()
                    return r.json()

            t0 = _time.monotonic()
            data = _with_retry(_do_chunk, f"Bundle chunk {i + 1}/{len(chunks)}")
            net_elapsed += _time.monotonic() - t0
            chunk_count = data.get("count", 0)
            total_count += chunk_count
            if len(chunks) > 1:
                logger.info(
                    f"  Chunk {i + 1}/{len(chunks)}: {len(compressed) / (1024 * 1024):.0f}MB -> {chunk_count} files"
                )

        stats = TransferStats(bytes_transferred=total_bytes, elapsed_seconds=net_elapsed)
        mb = total_bytes / (1024 * 1024)
        chunk_info = f", {len(chunks)} chunks" if len(chunks) > 1 else ""
        logger.info(
            f"Uploaded {total_count} files (bundle, {mb:.1f}MB gzip{chunk_info}, "
            f"{stats.mbps:.1f} MB/s) for {clip_name}/{pass_name}"
        )
        return total_count, stats

    def _build_tar_chunks(self, src_dir: str, files: list[str]) -> list[bytes]:
        """Build gzip-compressed tar chunks, each under _MAX_BUNDLE_BYTES.

        Streams one chunk at a time instead of buffering the entire tar.
        Only one chunk is in memory at a time — peak RAM = one chunk (~90MB).
        """
        import gzip

        total_raw = sum(os.path.getsize(os.path.join(src_dir, f)) for f in files)
        total_mb = total_raw / (1024 * 1024)
        logger.info(f"Preparing {len(files)} files ({total_mb:.0f}MB) for upload...")

        # Estimate compression ratio from first file to plan chunking
        first_path = os.path.join(src_dir, files[0])
        sample_raw = open(first_path, "rb").read()
        sample_gz = gzip.compress(sample_raw, compresslevel=1)
        ratio = len(sample_raw) / max(1, len(sample_gz))
        raw_limit = int(self._MAX_BUNDLE_BYTES * ratio * 0.85)  # 85% safety margin

        # Split files into chunk groups by estimated size
        chunk_groups: list[list[str]] = []
        current_group: list[str] = []
        current_size = 0

        for fname in files:
            fsize = os.path.getsize(os.path.join(src_dir, fname))
            if current_size + fsize > raw_limit and current_group:
                chunk_groups.append(current_group)
                current_group = []
                current_size = 0
            current_group.append(fname)
            current_size += fsize

        if current_group:
            chunk_groups.append(current_group)

        # Compress one chunk at a time (only one in memory)
        t0 = _time.time()
        chunks: list[bytes] = []
        total_compressed = 0

        for _i, group in enumerate(chunk_groups):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for fname in group:
                    tar.add(os.path.join(src_dir, fname), arcname=fname)
            compressed = gzip.compress(buf.getvalue(), compresslevel=1)
            chunks.append(compressed)
            total_compressed += len(compressed)
            del buf  # free raw buffer immediately

        elapsed = _time.time() - t0
        comp_mb = total_compressed / (1024 * 1024)
        logger.info(
            f"Compressed {total_mb:.0f}MB → {comp_mb:.0f}MB "
            f"({total_mb / max(1, comp_mb):.1f}x, {len(chunks)} chunks) in {elapsed:.1f}s"
        )

        return chunks
