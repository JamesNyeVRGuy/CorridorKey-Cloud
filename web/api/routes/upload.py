"""Upload endpoints — video files, image sequences (zip), and alpha hints."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from backend.job_queue import GPUJob, JobType
from backend.project import (
    create_project,
    is_image_file,
    is_video_file,
    sanitize_stem,
)

from ..deps import get_queue, get_service
from ..org_isolation import resolve_clips_dir
from ..path_security import safe_extract_zip
from ..routes import clips as _clips_mod
from ..storage_quota import check_storage_quota, finish_upload
from ..tier_guard import require_member

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/upload", tags=["upload"], dependencies=[Depends(require_member)])

# Max upload size in bytes. Default 10 GB. Set CK_MAX_UPLOAD_MB to override.
_MAX_UPLOAD_BYTES = int(os.environ.get("CK_MAX_UPLOAD_MB", "10240").strip()) * 1024 * 1024
_CHUNK_SIZE = int(os.environ.get("CK_CHUNK_SIZE_MB", "10").strip()) * 1024 * 1024

async def _save_upload(file: UploadFile, dest: str) -> None:
    """Save an uploaded file to dest with size limit enforcement."""
    total = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(_CHUNK_SIZE):
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                f.close()
                os.unlink(dest)
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload exceeds maximum size ({_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
                )
            f.write(chunk)


@router.post("/video", summary="Upload video file")
async def upload_video(
    file: UploadFile,
    request: Request,
    name: str | None = None,
    auto_extract: bool = True,
    project: str | None = None,
    folder: str | None = None,
):
    """Upload a video file. Adds to existing project if specified, else creates new.

    When `project` is set, the clip is added to that existing project
    (optionally inside `folder`). Otherwise creates a new project.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    safe_name = os.path.basename(file.filename)
    if not is_video_file(safe_name):
        ext = os.path.splitext(safe_name)[1]
        raise HTTPException(
            status_code=400,
            detail=f"Not a supported video format (got {ext}). Supported: .mp4, .mov, .avi, .mkv, .mxf, .webm, .m4v",
        )

    check_storage_quota(request)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = os.path.join(tmpdir, safe_name)
            try:
                await _save_upload(file, tmp_path)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail="Failed to save upload") from e

            # Validate video resolution, framerate, duration against tier limits (CRKY-146)
            if is_video_file(tmp_path):
                try:
                    from backend.ffmpeg_tools import probe_video

                    from ..tier_limits import check_video_limits

                    video_info = probe_video(tmp_path)
                    check_video_limits(request, video_info)
                except HTTPException:
                    raise
                except Exception:
                    pass  # ffprobe not available — skip validation

            try:
                clips_dir = resolve_clips_dir(request)
                if project:
                    # Add to existing project
                    project_dir = os.path.join(clips_dir, project)
                    if not os.path.isdir(project_dir):
                        raise HTTPException(status_code=404, detail=f"Project '{project}' not found")
                    from backend.project import add_clips_to_project

                    add_clips_to_project(project_dir, [tmp_path], copy_source=True, folder_name=folder)
                else:
                    project_dir = create_project(
                        tmp_path,
                        copy_source=True,
                        display_name=name,
                        root_dir=clips_dir,
                    )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail="Failed to create project") from e

        # Scan the new clips
        service = get_service()
        clips_dir = resolve_clips_dir(request)
        clips = service.scan_clips(clips_dir)
        new_clips = [c for c in clips if c.root_path.startswith(project_dir)]

        # Auto-submit extraction jobs for any clip with a video source
        extract_jobs = []
        if auto_extract:
            queue = get_queue()
            for clip in new_clips:
                has_video = clip.input_asset and clip.input_asset.asset_type == "video"
                no_frames = not os.path.isdir(os.path.join(clip.root_path, "Frames"))
                logger.info(
                    f"Upload auto-extract check: clip={clip.name} state={clip.state.value} "
                    f"has_video={has_video} no_frames={no_frames}"
                )
                if has_video or clip.state.value == "EXTRACTING":
                    job = GPUJob(job_type=JobType.VIDEO_EXTRACT, clip_name=clip.name)
                    # Stamp with user/org context so the worker finds the clip in the right org dir
                    from ..auth import get_current_user

                    user = get_current_user(request)
                    if user:
                        job.submitted_by = user.user_id
                        active_org = request.headers.get("X-Org-Id", "").strip()
                        if active_org:
                            job.org_id = active_org
                        else:
                            from ..orgs import get_org_store

                            user_orgs = get_org_store().list_user_orgs(user.user_id)
                            job.org_id = user_orgs[0].org_id if user_orgs else None
                    if queue.submit(job):
                        extract_jobs.append(job.id)
                        logger.info(f"Auto-queued extraction job {job.id} for '{clip.name}'")

        return {
            "status": "ok",
            "clips": [_clips_mod._clip_to_schema(c) for c in new_clips],
            "extract_jobs": extract_jobs,
        }
    finally:
        finish_upload(request)


@router.post("/frames", summary="Upload image sequence (ZIP)")
async def upload_frames(file: UploadFile, request: Request, name: str | None = None):
    """Upload a zip of image frames to create a new clip.

    The zip should contain image files (PNG, EXR, JPG, etc.) at the
    top level or in a single subdirectory. They'll be placed into
    a new project's Frames/ directory.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    safe_name = os.path.basename(file.filename)
    if not safe_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Expected a .zip file containing image frames")

    check_storage_quota(request)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, safe_name)
            try:
                await _save_upload(file, zip_path)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail="Failed to save upload") from e

            # Extract zip
            extract_dir = os.path.join(tmpdir, "extracted")
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    safe_extract_zip(zf, extract_dir)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid zip file") from None

            # Find image files — may be at root or in a single subdirectory
            image_files = [f for f in os.listdir(extract_dir) if is_image_file(f)]
            if not image_files:
                subdirs = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
                if len(subdirs) == 1:
                    subdir_path = os.path.join(extract_dir, subdirs[0])
                    image_files = [f for f in os.listdir(subdir_path) if is_image_file(f)]
                    if image_files:
                        extract_dir = subdir_path

            if not image_files:
                raise HTTPException(status_code=400, detail="No image files found in zip")

            # Create project structure manually (create_project expects video)
            from datetime import datetime

            from backend.project import _dedupe_path, write_clip_json, write_project_json

            clip_name = sanitize_stem(name or safe_name)
            timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
            folder_name = f"{timestamp}_{clip_name}"

            root = resolve_clips_dir(request)
            project_dir, _ = _dedupe_path(root, folder_name)
            clips_dir = os.path.join(project_dir, "clips")
            clip_dir, clip_name = _dedupe_path(clips_dir, clip_name)
            frames_dir = os.path.join(clip_dir, "Frames")
            os.makedirs(frames_dir, exist_ok=True)

            for fname in sorted(image_files):
                src = os.path.join(extract_dir, fname)
                dst = os.path.join(frames_dir, fname)
                shutil.copy2(src, dst)

            write_clip_json(clip_dir, {"source": {"type": "uploaded_frames", "original_filename": safe_name}})
            write_project_json(
                project_dir,
                {
                    "version": 2,
                    "created": datetime.now().isoformat(),
                    "display_name": clip_name.replace("_", " "),
                    "clips": [clip_name],
                },
            )

        service = get_service()
        clips = service.scan_clips(resolve_clips_dir(request))
        new_clips = [c for c in clips if c.root_path.startswith(project_dir)]

        return {
            "status": "ok",
            "clips": [_clips_mod._clip_to_schema(c) for c in new_clips],
            "frame_count": len(image_files),
        }
    finally:
        finish_upload(request)


@router.post("/images", summary="Upload image files")
async def upload_images(
    request: Request,
    files: list[UploadFile],
    name: str | None = None,
    project: str | None = None,
    folder: str | None = None,
):
    """Upload one or more image files directly (no zip required).

    Accepts PNG, JPG, EXR, TIFF, BMP, DPX files. When `project` is set,
    adds to that existing project. Otherwise creates a new project.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    # Validate all files are images
    for f in files:
        if not f.filename:
            raise HTTPException(status_code=400, detail="File missing filename")
        safe_name = os.path.basename(f.filename)
        if not is_image_file(safe_name):
            ext = os.path.splitext(safe_name)[1]
            raise HTTPException(
                status_code=400,
                detail=f"Not a supported image format (got {ext}). "
                "Supported: .png, .jpg, .jpeg, .exr, .tif, .tiff, .bmp, .dpx",
            )

    check_storage_quota(request)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Save all uploaded images to temp dir
            saved_files: list[str] = []
            for f in files:
                safe_name = os.path.basename(f.filename or "frame.png")
                tmp_path = os.path.join(tmpdir, safe_name)
                try:
                    await _save_upload(f, tmp_path)
                    saved_files.append(safe_name)
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(status_code=500, detail="Failed to save upload") from e

            if not saved_files:
                raise HTTPException(status_code=400, detail="No valid image files")

            root = resolve_clips_dir(request)

            if project:
                # Add to existing project
                project_dir = os.path.join(root, project)
                if not os.path.isdir(project_dir):
                    raise HTTPException(status_code=404, detail=f"Project '{project}' not found")

                from backend.project import _dedupe_path, write_clip_json

                first_name = name or os.path.splitext(saved_files[0])[0]
                clip_name = sanitize_stem(first_name)
                parent = os.path.join(project_dir, "folders", folder) if folder else os.path.join(project_dir, "clips")
                clip_dir, clip_name = _dedupe_path(parent, clip_name)
                frames_dir = os.path.join(clip_dir, "Frames")
                os.makedirs(frames_dir, exist_ok=True)

                for fname in sorted(saved_files):
                    shutil.copy2(os.path.join(tmpdir, fname), os.path.join(frames_dir, fname))

                source_type = "uploaded_image" if len(saved_files) == 1 else "uploaded_frames"
                write_clip_json(clip_dir, {"source": {"type": source_type, "file_count": len(saved_files)}})
            else:
                # Create new project
                from datetime import datetime

                from backend.project import _dedupe_path, write_clip_json, write_project_json

                first_name = name or os.path.splitext(saved_files[0])[0]
                clip_name = sanitize_stem(first_name)
                timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
                folder_name = f"{timestamp}_{clip_name}"

                project_dir, _ = _dedupe_path(root, folder_name)
                clips_subdir = os.path.join(project_dir, "clips")
                clip_dir, clip_name = _dedupe_path(clips_subdir, clip_name)
                frames_dir = os.path.join(clip_dir, "Frames")
                os.makedirs(frames_dir, exist_ok=True)

                for fname in sorted(saved_files):
                    shutil.copy2(os.path.join(tmpdir, fname), os.path.join(frames_dir, fname))

                source_type = "uploaded_image" if len(saved_files) == 1 else "uploaded_frames"
                write_clip_json(clip_dir, {"source": {"type": source_type, "file_count": len(saved_files)}})
                write_project_json(
                    project_dir,
                    {
                        "version": 2,
                        "created": datetime.now().isoformat(),
                        "display_name": clip_name.replace("_", " "),
                        "clips": [clip_name],
                    },
                )

        service = get_service()
        clips = service.scan_clips(resolve_clips_dir(request))
        new_clips = [c for c in clips if c.root_path.startswith(project_dir)]

        return {
            "status": "ok",
            "clips": [_clips_mod._clip_to_schema(c) for c in new_clips],
            "frame_count": len(saved_files),
        }
    finally:
        finish_upload(request)


@router.post("/alpha/{clip_name}", summary="Upload alpha hint frames")
async def upload_alpha_hint(clip_name: str, file: UploadFile, request: Request):
    """Upload alpha hint frames (zip) for an existing clip.

    Extracts images into the clip's AlphaHint/ directory.
    Transitions clip from RAW -> READY.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    safe_name = os.path.basename(file.filename)
    if not safe_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Expected a .zip file containing alpha hint frames")

    service = get_service()
    clips = service.scan_clips(resolve_clips_dir(request))
    clip = next((c for c in clips if c.name == clip_name), None)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    check_storage_quota(request)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, safe_name)
            try:
                await _save_upload(file, zip_path)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail="Failed to save upload") from e

            extract_dir = os.path.join(tmpdir, "extracted")
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    safe_extract_zip(zf, extract_dir)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid zip file") from None

            image_files = [f for f in os.listdir(extract_dir) if is_image_file(f)]
            if not image_files:
                subdirs = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
                if len(subdirs) == 1:
                    extract_dir = os.path.join(extract_dir, subdirs[0])
                    image_files = [f for f in os.listdir(extract_dir) if is_image_file(f)]

            if not image_files:
                raise HTTPException(status_code=400, detail="No image files found in zip")

            alpha_dir = os.path.join(clip.root_path, "AlphaHint")
            os.makedirs(alpha_dir, exist_ok=True)

            for fname in sorted(image_files):
                src = os.path.join(extract_dir, fname)
                dst = os.path.join(alpha_dir, fname)
                shutil.copy2(src, dst)

        clips = service.scan_clips(resolve_clips_dir(request))
        updated = next((c for c in clips if c.name == clip_name), None)

        return {
            "status": "ok",
            "clip": _clips_mod._clip_to_schema(updated) if updated else None,
            "alpha_frames": len(image_files),
        }
    finally:
        finish_upload(request)


@router.post("/mask/{clip_name}", summary="Upload VideoMaMa mask frames")
async def upload_videomama_mask(clip_name: str, file: UploadFile, request: Request):
    """Upload VideoMaMa mask hint frames (zip) for an existing clip.

    Extracts images into the clip's VideoMamaMaskHint/ directory.
    Transitions clip to MASKED state.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    safe_name = os.path.basename(file.filename)
    if not safe_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Expected a .zip file containing mask frames")

    service = get_service()
    clips = service.scan_clips(resolve_clips_dir(request))
    clip = next((c for c in clips if c.name == clip_name), None)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_name}' not found")

    check_storage_quota(request)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, safe_name)
            try:
                await _save_upload(file, zip_path)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail="Failed to save upload") from e

            extract_dir = os.path.join(tmpdir, "extracted")
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    safe_extract_zip(zf, extract_dir)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid zip file") from None

            image_files = [f for f in os.listdir(extract_dir) if is_image_file(f)]
            if not image_files:
                subdirs = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
                if len(subdirs) == 1:
                    extract_dir = os.path.join(extract_dir, subdirs[0])
                    image_files = [f for f in os.listdir(extract_dir) if is_image_file(f)]

            if not image_files:
                raise HTTPException(status_code=400, detail="No image files found in zip")

            mask_dir = os.path.join(clip.root_path, "VideoMamaMaskHint")
            os.makedirs(mask_dir, exist_ok=True)

            for fname in sorted(image_files):
                src = os.path.join(extract_dir, fname)
                dst = os.path.join(mask_dir, fname)
                shutil.copy2(src, dst)

        clips = service.scan_clips(resolve_clips_dir(request))
        updated = next((c for c in clips if c.name == clip_name), None)

        return {
            "status": "ok",
            "clip": _clips_mod._clip_to_schema(updated) if updated else None,
            "mask_frames": len(image_files),
        }
    finally:
        finish_upload(request)
