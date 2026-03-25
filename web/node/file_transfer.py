"""File transfer utilities for nodes without shared storage.

Downloads input frames from the main machine and uploads results back.
Uses tar bundle downloads for speed, with per-file fallback.
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
import threading
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Max concurrent file transfers across all nodes on this machine.
# Prevents multiple jobs from saturating the network simultaneously.
_transfer_semaphore = threading.Semaphore(2)

# Transient HTTP status codes worth retrying (server restart, load balancer hiccup)
_RETRY_STATUSES = {401, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_DELAY = 3  # seconds


def _with_retry(fn, description: str = "request"):
    """Retry a function on transient HTTP errors."""
    import time

    last_err = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                logger.warning(f"{description}: {e.response.status_code}, retrying in {_RETRY_DELAY}s ({attempt + 1}/{_MAX_RETRIES})")
                time.sleep(_RETRY_DELAY)
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
    ) -> int:
        """Download files for a clip pass into the correct subdirectory.

        Tries tar bundle download first (single HTTP request for all files),
        falls back to per-file download if the bundle endpoint isn't available.

        Args:
            clip_name: Name of the clip.
            pass_name: Pass type ("input", "alpha", "mask", "source").
            clip_dir: Local clip root directory (files go into a subdirectory).
            frame_range: Optional (start, end) to only download frames in range.

        Returns the number of files downloaded.
        """
        # Try bundle download first
        try:
            count = self._download_bundle(clip_name, pass_name, clip_dir, frame_range)
            if count > 0:
                return count
        except Exception as e:
            logger.debug(f"Bundle download failed, falling back to per-file: {e}")

        return self._download_per_file(clip_name, pass_name, clip_dir, frame_range)

    def _download_bundle(
        self, clip_name: str, pass_name: str, clip_dir: str, frame_range: tuple[int, int] | None
    ) -> int:
        """Download files as a tar stream (single HTTP request)."""
        params = {}
        if frame_range:
            params["start"] = frame_range[0]
            params["end"] = frame_range[1]

        url = self._url(f"{clip_name}/{pass_name}/bundle")
        with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            with client.stream("GET", url, params=params) as resp:
                if resp.status_code != 200:
                    return 0

                # Stream tar data and extract
                buf = io.BytesIO()
                for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                    buf.write(chunk)
                buf.seek(0)

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

        directory = resp.headers.get("x-tar-directory", pass_name)
        logger.info(f"Downloaded {count} files (bundle) for {clip_name}/{pass_name} → {directory}/")
        return count

    def _download_per_file(
        self, clip_name: str, pass_name: str, clip_dir: str, frame_range: tuple[int, int] | None
    ) -> int:
        """Download files one at a time (fallback)."""
        directory, files = self.list_files(clip_name, pass_name)
        if not files or not directory:
            return 0

        if frame_range is not None:
            start, end = frame_range
            files = files[start:end]

        dest_dir = os.path.join(clip_dir, directory)
        os.makedirs(dest_dir, exist_ok=True)
        count = 0

        for fname in files:
            dest_path = os.path.join(dest_dir, fname)
            if os.path.isfile(dest_path):
                count += 1
                continue

            url = self._url(f"{clip_name}/{pass_name}/{fname}")
            tmp_path = dest_path + ".part"

            def _do_download(u=url, tp=tmp_path, dp=dest_path):
                with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
                    try:
                        with client.stream("GET", u) as resp:
                            resp.raise_for_status()
                            with open(tp, "wb") as f:
                                for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                                    f.write(chunk)
                        os.replace(tp, dp)
                    except Exception:
                        if os.path.isfile(tp):
                            os.remove(tp)
                        raise

            _with_retry(_do_download, f"Download {fname}")
            count += 1

        logger.info(f"Downloaded {count} files for {clip_name}/{pass_name} → {directory}/")
        return count

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

    def upload_directory(self, clip_name: str, pass_name: str, src_dir: str) -> int:
        """Upload all files in a directory as results.

        Tries tar bundle upload first (single HTTP request), falls back
        to per-file upload if bundle endpoint isn't available.

        Returns the number of files uploaded.
        """
        if not os.path.isdir(src_dir):
            return 0

        files = sorted(f for f in os.listdir(src_dir) if os.path.isfile(os.path.join(src_dir, f)))
        if not files:
            return 0

        # Try bundle upload first
        try:
            count = self._upload_bundle(clip_name, pass_name, src_dir, files)
            if count > 0:
                return count
        except Exception as e:
            logger.warning(f"Bundle upload failed, falling back to per-file: {e}")

        # Per-file fallback
        count = 0
        for fname in files:
            fpath = os.path.join(src_dir, fname)
            self.upload_file(clip_name, pass_name, fpath)
            count += 1

        logger.info(f"Uploaded {count} files (per-file) for {clip_name}/{pass_name}")
        return count

    # Max compressed bundle size per upload (stay under Cloudflare's 100MB limit)
    _MAX_BUNDLE_BYTES = 90 * 1024 * 1024

    def _upload_bundle(self, clip_name: str, pass_name: str, src_dir: str, files: list[str]) -> int:
        """Upload files as gzip-compressed tar chunks.

        Splits into multiple uploads if the compressed size exceeds 90MB
        (Cloudflare's 100MB upload limit). Each chunk is a complete tar
        that the server extracts independently.
        """
        import gzip

        # Build tar in memory and check size — split into chunks if needed
        chunks = self._build_tar_chunks(src_dir, files)

        total_count = 0
        total_bytes = 0
        url = self._url(f"{clip_name}/{pass_name}/bundle")

        for i, compressed in enumerate(chunks):
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

            data = _with_retry(_do_chunk, f"Bundle chunk {i + 1}/{len(chunks)}")
            chunk_count = data.get("count", 0)
            total_count += chunk_count
            if len(chunks) > 1:
                logger.info(f"  Chunk {i + 1}/{len(chunks)}: {len(compressed) / (1024*1024):.0f}MB → {chunk_count} files")

        mb = total_bytes / (1024 * 1024)
        chunk_info = f", {len(chunks)} chunks" if len(chunks) > 1 else ""
        logger.info(f"Uploaded {total_count} files (bundle, {mb:.1f}MB gzip{chunk_info}) for {clip_name}/{pass_name}")
        return total_count

    def _build_tar_chunks(self, src_dir: str, files: list[str]) -> list[bytes]:
        """Build gzip-compressed tar chunks, each under _MAX_BUNDLE_BYTES."""
        import gzip

        import time as _time

        # Try single bundle first
        logger.info(f"Compressing {len(files)} files for upload...")
        t0 = _time.time()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for fname in files:
                fpath = os.path.join(src_dir, fname)
                tar.add(fpath, arcname=fname)
        raw = buf.getvalue()
        compressed = gzip.compress(raw, compresslevel=1)
        elapsed = _time.time() - t0
        raw_mb = len(raw) / (1024 * 1024)
        comp_mb = len(compressed) / (1024 * 1024)
        logger.info(f"Compressed {raw_mb:.0f}MB → {comp_mb:.0f}MB ({raw_mb / max(1, comp_mb):.1f}x) in {elapsed:.1f}s")

        if len(compressed) <= self._MAX_BUNDLE_BYTES:
            return [compressed]

        # Too large — split files into chunks that fit
        logger.info(f"Bundle {comp_mb:.0f}MB exceeds {self._MAX_BUNDLE_BYTES // (1024*1024)}MB limit, splitting into chunks")
        chunks = []
        chunk_files: list[str] = []
        chunk_raw_size = 0

        # Estimate compression ratio from the full bundle
        ratio = len(raw) / max(1, len(compressed))
        raw_limit = int(self._MAX_BUNDLE_BYTES * ratio * 0.9)  # 90% of estimated limit

        for fname in files:
            fpath = os.path.join(src_dir, fname)
            fsize = os.path.getsize(fpath)

            if chunk_raw_size + fsize > raw_limit and chunk_files:
                # Flush current chunk
                chunks.append(self._compress_chunk(src_dir, chunk_files))
                chunk_files = []
                chunk_raw_size = 0

            chunk_files.append(fname)
            chunk_raw_size += fsize

        if chunk_files:
            chunks.append(self._compress_chunk(src_dir, chunk_files))

        return chunks

    def _compress_chunk(self, src_dir: str, files: list[str]) -> bytes:
        """Build and compress a single tar chunk."""
        import gzip

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for fname in files:
                tar.add(os.path.join(src_dir, fname), arcname=fname)
        return gzip.compress(buf.getvalue(), compresslevel=1)
