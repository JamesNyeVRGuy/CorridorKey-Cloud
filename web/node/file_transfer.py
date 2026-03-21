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


class FileTransfer:
    """Handles file downloads/uploads between node and main machine."""

    def __init__(self, main_url: str, node_id: str, timeout: float = 300, auth_token: str = ""):
        self.main_url = main_url.rstrip("/")
        self.node_id = node_id
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    def _url(self, path: str) -> str:
        return f"{self.main_url}/api/nodes/{self.node_id}/files/{path}"

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

        with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            for fname in files:
                dest_path = os.path.join(dest_dir, fname)
                if os.path.isfile(dest_path):
                    count += 1
                    continue

                url = self._url(f"{clip_name}/{pass_name}/{fname}")
                tmp_path = dest_path + ".part"
                try:
                    with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                                f.write(chunk)
                    os.replace(tmp_path, dest_path)
                except Exception:
                    if os.path.isfile(tmp_path):
                        os.remove(tmp_path)
                    raise
                count += 1

        logger.info(f"Downloaded {count} files for {clip_name}/{pass_name} → {directory}/")
        return count

    def upload_file(self, clip_name: str, pass_name: str, file_path: str) -> None:
        """Upload a single result file to the main machine."""
        fname = Path(file_path).name
        url = self._url(f"{clip_name}/{pass_name}/{fname}")

        with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            with open(file_path, "rb") as f:
                r = client.post(url, files={"file": (fname, f)})
                r.raise_for_status()

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

    def _upload_bundle(self, clip_name: str, pass_name: str, src_dir: str, files: list[str]) -> int:
        """Upload files as a tar stream (single HTTP request)."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w|") as tar:
            for fname in files:
                fpath = os.path.join(src_dir, fname)
                tar.add(fpath, arcname=fname)
        buf.seek(0)

        url = self._url(f"{clip_name}/{pass_name}/bundle")
        with _transfer_semaphore, httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            r = client.post(
                url,
                content=buf.read(),
                headers={**self._headers, "Content-Type": "application/x-tar"},
            )
            r.raise_for_status()
            data = r.json()
            count = data.get("count", len(files))

        logger.info(f"Uploaded {count} files (bundle) for {clip_name}/{pass_name}")
        return count
