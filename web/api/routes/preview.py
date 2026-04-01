"""Preview endpoint — serves frames as PNG, preview videos as MP4, and downloads as ZIP."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import tempfile
import threading
import time
import zipfile

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from starlette.responses import StreamingResponse

from backend.frame_io import read_image_frame
from backend.natural_sort import natsorted
from backend.project import is_image_file

from ..deps import get_service
from ..org_isolation import resolve_clips_dir
from ..tier_guard import require_member

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/preview", tags=["preview"], dependencies=[Depends(require_member)])

_clips_dir: str = ""
# Cache dir for stitched preview videos
_cache_dir: str = ""

# Track video encode progress: key → {status, current, total, error}
_encode_progress: dict[str, dict] = {}
_encode_progress_lock = threading.Lock()


def set_clips_dir(path: str) -> None:
    global _clips_dir, _cache_dir
    _clips_dir = path
    _cache_dir = os.path.join(path, ".cache", "preview_videos")
    os.makedirs(_cache_dir, exist_ok=True)


_PASS_MAP = {
    "input": "Input",
    "frames": "Frames",
    "alpha": "AlphaHint",
    "fg": "Output/FG",
    "matte": "Output/Matte",
    "comp": "Output/Comp",
    "processed": "Output/Processed",
}


def _find_clip_root(clip_name: str, clips_dir: str | None = None) -> str | None:
    service = get_service()
    clips = service.scan_clips(clips_dir or _clips_dir)
    for clip in clips:
        if clip.name == clip_name:
            return clip.root_path
    return None


def _resolve_pass_dir(clip_root: str, pass_name: str) -> str:
    """Resolve the directory for a pass, handling input/frames fallback."""
    if pass_name == "input":
        frames_dir = os.path.join(clip_root, "Frames")
        input_dir = os.path.join(clip_root, "Input")
        if os.path.isdir(frames_dir) and os.listdir(frames_dir):
            return frames_dir
        elif os.path.isdir(input_dir):
            return input_dir
        raise HTTPException(status_code=404, detail="No input frames directory found")
    target = os.path.join(clip_root, _PASS_MAP[pass_name])
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail=f"Directory not found: {_PASS_MAP[pass_name]}")
    return target


def _frame_to_png_bytes(img: np.ndarray) -> bytes:
    if img.dtype == np.float32 or img.dtype == np.float64:
        img = (np.clip(img, 0.0, 1.0) * 255.0).astype(np.uint8)
    if img.ndim == 3 and img.shape[2] >= 3:
        img_bgr = cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2BGR)
    elif img.ndim == 3 and img.shape[2] == 4:
        img_bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA)
    elif img.ndim == 2:
        img_bgr = img
    else:
        img_bgr = img
    success, buf = cv2.imencode(".png", img_bgr)
    if not success:
        raise RuntimeError("PNG encode failed")
    return buf.tobytes()


# --- Single frame preview ---


def _resize_if_needed(img: np.ndarray, width: int | None) -> np.ndarray:
    """Downscale image to target width, preserving aspect ratio. No-op if width is None or larger than image."""
    if width is None or width <= 0:
        return img
    h, w = img.shape[:2]
    if width >= w:
        return img
    new_h = int(h * width / w)
    return cv2.resize(img, (width, new_h), interpolation=cv2.INTER_AREA)


@router.get("/{clip_name}/{pass_name}/{frame:int}")
def get_preview_frame(clip_name: str, pass_name: str, frame: int, request: Request, width: int | None = None):
    if pass_name not in _PASS_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown pass: {pass_name}. Valid: {list(_PASS_MAP.keys())}")

    clip_root = _find_clip_root(clip_name, resolve_clips_dir(request))
    if clip_root is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    target_dir = _resolve_pass_dir(clip_root, pass_name)
    files = natsorted([f for f in os.listdir(target_dir) if is_image_file(f)])
    if not files:
        raise HTTPException(status_code=404, detail=f"No frames in {pass_name}")
    if frame < 0 or frame >= len(files):
        raise HTTPException(status_code=404, detail=f"Frame {frame} out of range (0-{len(files) - 1})")

    fpath = os.path.join(target_dir, files[frame])

    if pass_name == "matte":
        os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"
        img = cv2.imread(fpath, cv2.IMREAD_ANYDEPTH | cv2.IMREAD_UNCHANGED)
        if img is None:
            raise HTTPException(status_code=500, detail="Failed to read matte frame")
        if img.ndim == 3:
            img = img[:, :, 0]
        if img.dtype != np.uint8:
            img = (np.clip(img, 0.0, 1.0) * 255.0).astype(np.uint8)
        img = _resize_if_needed(img, width)
        success, buf = cv2.imencode(".png", img)
        if not success:
            raise HTTPException(status_code=500, detail="PNG encode failed")
        return Response(content=buf.tobytes(), media_type="image/png")

    img = read_image_frame(fpath)
    if img is None:
        raise HTTPException(status_code=500, detail="Failed to read frame")

    img = _resize_if_needed(img, width)
    return Response(content=_frame_to_png_bytes(img), media_type="image/png")


# --- Video preview (stitched MP4) ---

# Lock to prevent concurrent ffmpeg encodes for the same cache key
_encode_locks: dict[str, threading.Lock] = {}
_encode_locks_lock = threading.Lock()


def _get_encode_lock(key: str) -> threading.Lock:
    with _encode_locks_lock:
        if key not in _encode_locks:
            _encode_locks[key] = threading.Lock()
        return _encode_locks[key]


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _cache_key(clip_root: str, pass_name: str) -> str:
    """Generate a cache key based on directory path and modification time."""
    target_dir = _resolve_pass_dir(clip_root, pass_name)
    files = [f for f in os.listdir(target_dir) if is_image_file(f)]
    # Hash based on dir path, file count, and newest mtime
    newest = max((os.path.getmtime(os.path.join(target_dir, f)) for f in files), default=0)
    raw = f"{target_dir}:{len(files)}:{newest}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


@router.get("/{clip_name}/{pass_name}/video/progress")
def get_video_progress(clip_name: str, pass_name: str, request: Request, fps: int = Query(24, ge=1, le=120)):
    """Check video encode progress. Returns status, current frame, total frames."""
    clip_root = _find_clip_root(clip_name, resolve_clips_dir(request))
    if clip_root is None:
        return {"status": "error", "detail": "Clip not found"}

    key = _cache_key(clip_root, pass_name)
    cache_path = os.path.join(_cache_dir, f"{clip_name}_{pass_name}_{key}.mp4")

    if os.path.isfile(cache_path):
        return {"status": "ready"}

    with _encode_progress_lock:
        progress = _encode_progress.get(key)

    if progress is None:
        return {"status": "idle"}
    return progress


@router.get("/{clip_name}/{pass_name}/video")
def get_preview_video(clip_name: str, pass_name: str, request: Request, fps: int = Query(24, ge=1, le=120)):
    """Stitch frames into an MP4 for smooth browser playback. Cached."""
    if not _ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg not available — cannot generate preview video")

    if pass_name not in _PASS_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown pass: {pass_name}")

    clip_root = _find_clip_root(clip_name, resolve_clips_dir(request))
    if clip_root is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    target_dir = _resolve_pass_dir(clip_root, pass_name)
    files = natsorted([f for f in os.listdir(target_dir) if is_image_file(f)])
    if not files:
        raise HTTPException(status_code=404, detail=f"No frames in {pass_name}")

    # Check cache
    key = _cache_key(clip_root, pass_name)
    cache_path = os.path.join(_cache_dir, f"{clip_name}_{pass_name}_{key}.mp4")

    if os.path.isfile(cache_path):
        return FileResponse(cache_path, media_type="video/mp4", filename=f"{clip_name}_{pass_name}.mp4")

    # Serialize encodes per cache key to prevent duplicate ffmpeg processes
    encode_lock = _get_encode_lock(key)
    if not encode_lock.acquire(timeout=0.1):
        # Another thread is encoding this exact video — wait for it
        encode_lock.acquire()
        encode_lock.release()
        if os.path.isfile(cache_path):
            return FileResponse(cache_path, media_type="video/mp4", filename=f"{clip_name}_{pass_name}.mp4")
        raise HTTPException(status_code=500, detail="Concurrent encode failed")

    total_frames = len(files)
    with _encode_progress_lock:
        _encode_progress[key] = {"status": "encoding", "current": 0, "total": total_frames}

    concat_path = os.path.join(_cache_dir, f"{key}_concat.txt")
    try:
        with open(concat_path, "w") as f:
            for fname in files:
                fpath = os.path.join(target_dir, fname)
                # Escape special chars for ffmpeg concat format (backslash, single quote, newline)
                escaped = fpath.replace("\\", "\\\\").replace("'", "'\\''").replace("\n", "")
                f.write(f"file '{escaped}'\n")
                f.write(f"duration {1 / fps}\n")

        is_exr = files[0].lower().endswith(".exr")

        if is_exr:
            with tempfile.TemporaryDirectory() as tmpdir:
                for i, fname in enumerate(files):
                    fpath = os.path.join(target_dir, fname)
                    img = read_image_frame(fpath)
                    if img is not None:
                        out = (np.clip(img, 0.0, 1.0) * 255.0).astype(np.uint8)
                        out_bgr = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
                        cv2.imwrite(os.path.join(tmpdir, f"{i:06d}.png"), out_bgr)
                    with _encode_progress_lock:
                        _encode_progress[key] = {"status": "encoding", "current": i + 1, "total": total_frames}

                with _encode_progress_lock:
                    _encode_progress[key] = {"status": "stitching", "current": total_frames, "total": total_frames}

                cmd = [
                    "ffmpeg",
                    "-y",
                    "-framerate",
                    str(fps),
                    "-i",
                    os.path.join(tmpdir, "%06d.png"),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-crf",
                    "23",
                    "-movflags",
                    "+faststart",
                    cache_path,
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=300)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.decode()[-300:])
        else:
            with _encode_progress_lock:
                _encode_progress[key] = {"status": "stitching", "current": total_frames, "total": total_frames}
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "23",
                "-movflags",
                "+faststart",
                cache_path,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode()[-300:])

    except Exception as e:
        logger.error(f"Video stitch failed: {e}")
        with _encode_progress_lock:
            _encode_progress[key] = {"status": "error", "detail": "Video encoding failed"}
        raise HTTPException(status_code=500, detail="Failed to create preview video") from e
    finally:
        encode_lock.release()
        if os.path.isfile(concat_path):
            os.unlink(concat_path)

    with _encode_progress_lock:
        _encode_progress.pop(key, None)

    return FileResponse(cache_path, media_type="video/mp4", filename=f"{clip_name}_{pass_name}.mp4")


# --- Download (ZIP) with per-user throttling ---

_MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("CK_MAX_CONCURRENT_DOWNLOADS", "2"))
_DOWNLOAD_RATE_BYTES = int(os.environ.get("CK_DOWNLOAD_RATE_BYTES", str(10 * 1024 * 1024)))  # 10 MB/s default
_download_slots: dict[str, list[float]] = {}  # {user_id: [start_timestamps]}
_download_lock = threading.Lock()
_DOWNLOAD_SLOT_TIMEOUT = 600  # auto-release after 10 minutes


def _acquire_download_slot(user_id: str) -> None:
    """Acquire a download slot. Raises 429 if limit exceeded."""
    now = time.time()
    with _download_lock:
        slots = _download_slots.get(user_id, [])
        slots = [t for t in slots if now - t < _DOWNLOAD_SLOT_TIMEOUT]
        _download_slots[user_id] = slots
        if len(slots) >= _MAX_CONCURRENT_DOWNLOADS:
            raise HTTPException(
                status_code=429,
                detail=f"Too many concurrent downloads (max {_MAX_CONCURRENT_DOWNLOADS}). "
                "Wait for current downloads to finish.",
            )
        slots.append(now)


def _release_download_slot(user_id: str) -> None:
    """Release the oldest download slot."""
    with _download_lock:
        slots = _download_slots.get(user_id, [])
        if slots:
            slots.pop(0)
        if not slots:
            _download_slots.pop(user_id, None)


async def _throttled_file_stream(path: str, chunk_size: int = 256 * 1024):
    """Stream a file with rate limiting. Uses larger chunks for throughput."""
    bytes_this_second = 0
    second_start = time.monotonic()
    loop = asyncio.get_running_loop()
    f = open(path, "rb")  # noqa: SIM115
    try:
        while True:
            # Read in executor to avoid blocking the event loop
            chunk = await loop.run_in_executor(None, f.read, chunk_size)
            if not chunk:
                break
            yield chunk
            bytes_this_second += len(chunk)
            now = time.monotonic()
            elapsed = now - second_start
            if elapsed < 1.0 and bytes_this_second >= _DOWNLOAD_RATE_BYTES:
                await asyncio.sleep(1.0 - elapsed)
                bytes_this_second = 0
                second_start = time.monotonic()
            elif elapsed >= 1.0:
                bytes_this_second = 0
                second_start = now
    finally:
        f.close()


@router.get("/{clip_name}/{pass_name}/download")
def download_pass(clip_name: str, pass_name: str, request: Request):
    """Download all frames for a pass as a ZIP file."""
    if pass_name not in _PASS_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown pass: {pass_name}")

    clip_root = _find_clip_root(clip_name, resolve_clips_dir(request))
    if clip_root is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    target_dir = _resolve_pass_dir(clip_root, pass_name)
    files = natsorted(os.listdir(target_dir))
    files = [f for f in files if not f.startswith(".")]
    if not files:
        raise HTTPException(status_code=404, detail=f"No files in {pass_name}")

    zip_name = f"{clip_name}_{pass_name}.zip"

    # Build ZIP to a temp file FIRST (no download slot held during build)
    zip_path = os.path.join(_cache_dir, f"dl_{zip_name}")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in files:
                fpath = os.path.join(target_dir, fname)
                zf.write(fpath, arcname=os.path.join(pass_name, fname))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to create ZIP") from e

    file_size = os.path.getsize(zip_path)

    # Acquire download slot AFTER ZIP is built (only held during streaming)
    from ..auth import AUTH_ENABLED, get_current_user

    user_id = "anonymous"
    if AUTH_ENABLED:
        user = get_current_user(request)
        if user:
            user_id = user.user_id
    _acquire_download_slot(user_id)

    async def stream_and_release():
        try:
            async for chunk in _throttled_file_stream(zip_path):
                yield chunk
        finally:
            _release_download_slot(user_id)

    return StreamingResponse(
        stream_and_release(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "Content-Length": str(file_size),
        },
    )
