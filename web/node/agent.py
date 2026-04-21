"""Node agent — registers with the main machine, polls for jobs, processes them.

Supports multi-GPU: spawns one subprocess per GPU using the shared
gpu_subprocess module. Single-GPU mode runs inference in-process.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import httpx

from device_utils import check_gpu_available, check_gpu_torch_compat, enumerate_gpus, get_cpu_stats

from . import config
from .file_transfer import FileTransfer
from .log_buffer import buffer as log_buffer
from .weight_sync import sync_weights

# Use 'spawn' start method to avoid CUDA re-initialization errors on Linux/Docker.
# fork() copies the parent's CUDA context, which can't be re-initialized in children.
_mp = multiprocessing.get_context("spawn")

logger = logging.getLogger(__name__)


def _load_embedded_version() -> dict[str, str]:
    """Load version info from _version.env (embedded by CI in frozen builds)."""
    import sys

    # In frozen builds, _version.env is bundled by PyInstaller
    if getattr(sys, "frozen", False):
        version_path = os.path.join(sys._MEIPASS, "web", "node", "_version.env")
    else:
        version_path = os.path.join(os.path.dirname(__file__), "_version.env")

    if os.path.isfile(version_path):
        result = {}
        with open(version_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    result[k.strip()] = v.strip()
        return result
    return {}


_EMBEDDED_VERSION = _load_embedded_version()


def _get_local_version() -> str:
    """Detect version: embedded (frozen) > git (dev) > unknown."""
    v = _EMBEDDED_VERSION.get("CK_BUILD_COMMIT")
    if v:
        return v[:12]

    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _get_local_build_number() -> int:
    """Get build number: embedded (frozen) > git timestamp (dev) > 0."""
    bn = _EMBEDDED_VERSION.get("CK_BUILD_NUMBER")
    if bn:
        try:
            return int(bn)
        except ValueError:
            pass

    try:
        import subprocess

        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except Exception:
        return 0


class NodeAgent:
    """Lightweight agent that connects to the main CorridorKey server."""

    def __init__(self, tray=None):
        self.node_id = config.NODE_ID
        self.name = config.NODE_NAME
        self.main_url = config.MAIN_URL.rstrip("/")
        self.shared_storage = config.SHARED_STORAGE or None
        self.poll_interval = config.POLL_INTERVAL
        self.heartbeat_interval = config.HEARTBEAT_INTERVAL
        self.file_transfer = FileTransfer(self.main_url, self.node_id, auth_token=config.AUTH_TOKEN)
        self.tray = tray  # Optional TrayApp instance for status updates

        self._stop = threading.Event()
        self._dismissed = False  # Set when server returns 410 (explicitly removed)
        self._gpu_indices = self._resolve_gpus()
        self._busy_gpus: set[int] = set()  # GPU indices currently processing
        self._busy_lock = threading.Lock()

        if self.tray:
            self.tray.set_server_url(self.main_url)

    def _resolve_gpus(self) -> list[int]:
        """Determine which GPUs to use based on config."""
        if config.NODE_GPUS == "auto":
            gpus = enumerate_gpus()
            return [g.index for g in gpus] if gpus else [0]
        return [int(x.strip()) for x in config.NODE_GPUS.split(",") if x.strip()]

    def _prewarm(self) -> None:
        """Pre-load the CorridorKey model into VRAM to avoid cold-start delay."""
        logger.info("Pre-warming model into VRAM...")
        os.environ["CUDA_VISIBLE_DEVICES"] = str(self._gpu_indices[0])
        try:
            import time as _time

            t0 = _time.monotonic()
            from backend.service import CorridorKeyService

            svc = CorridorKeyService()
            svc.detect_device()
            # Access the engine to trigger model loading
            engine = svc._get_engine()
            self._model_compiled = getattr(engine, "compiled", False)
            elapsed = _time.monotonic() - t0
            logger.info(f"Model pre-warmed in {elapsed:.1f}s (compiled={self._model_compiled})")
        except Exception as e:
            logger.warning(f"Pre-warm failed (will load on first job): {e}")

    def _host_ip(self) -> str:
        """Best-effort local IP for registration."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _api(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if config.AUTH_TOKEN:
            headers["Authorization"] = f"Bearer {config.AUTH_TOKEN}"
        with httpx.Client(timeout=30, headers=headers) as client:
            return getattr(client, method)(f"{self.main_url}{path}", **kwargs)

    def _register(self) -> bool:
        """Register this node with the main server."""
        gpus_info = enumerate_gpus()
        gpu_slots = []
        for g in gpus_info:
            if g.index in self._gpu_indices:
                gpu_slots.append(
                    {
                        "index": g.index,
                        "name": g.name,
                        "vram_total_gb": g.vram_total_gb,
                        "vram_free_gb": g.vram_free_gb,
                    }
                )

        # Backward-compat: also fill legacy single-GPU fields from first GPU
        first_gpu = gpu_slots[0] if gpu_slots else {}

        payload = {
            "node_id": self.node_id,
            "name": self.name,
            "host": self._host_ip(),
            "gpus": gpu_slots,
            "gpu_name": first_gpu.get("name", ""),
            "vram_total_gb": first_gpu.get("vram_total_gb", 0),
            "vram_free_gb": first_gpu.get("vram_free_gb", 0),
            "capabilities": ["cuda"] if gpu_slots else ["cpu"],
            "model_compiled": getattr(self, "_model_compiled", False),
            "shared_storage": self.shared_storage,
            "accepted_types": [t.strip() for t in config.ACCEPTED_TYPES.split(",") if t.strip()],
            "security": {
                "running_as_root": getattr(os, "getuid", lambda: -1)() == 0,
                "hardened": os.environ.get("CK_NODE_HARDENED", "").strip().lower() in ("true", "1"),
                "uid": getattr(os, "getuid", lambda: -1)(),
                "read_only_fs": not os.access("/", os.W_OK),
                "agent_version": os.environ.get("CK_BUILD_COMMIT", "").strip() or _get_local_version(),
                "build_number": int(os.environ.get("CK_BUILD_NUMBER", "0").strip() or "0") or _get_local_build_number(),
            },
        }

        try:
            r = self._api("post", "/api/nodes/register", json=payload)
            r.raise_for_status()
            data = r.json()
            logger.info(f"Registered as '{self.name}' ({self.node_id}) with {len(gpu_slots)} GPU(s)")
            if self.tray:
                self.tray.set_status("idle")
                if gpu_slots:
                    g = gpu_slots[0]
                    self.tray.set_gpu_info(g.get("name", ""), g.get("vram_free_gb", 0))
            # Log any security warnings from the server
            for w in data.get("security_warnings", []):
                logger.warning(f"Server security warning: {w}")
            # Version mismatch warning
            if not data.get("version_match", True):
                server_v = data.get("server_version", "unknown")
                logger.warning(
                    f"Version mismatch — node: {payload['security']['agent_version']}, "
                    f"server: {server_v}. Consider updating the node."
                )
            return True
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", e.response.text)
            except Exception:
                detail = e.response.text
            logger.error(f"Registration failed ({e.response.status_code}): {detail}")
            return False
        except Exception as e:
            logger.error(f"Registration failed: {e}")
            return False

    def _check_gpu_ready(self) -> bool:
        """Check if our GPU is available (not in use by other processes)."""
        available, reason = check_gpu_available(self._gpu_indices[0])
        if not available:
            logger.debug(f"GPU not available: {reason}")
        return available

    def _heartbeat(self) -> bool:
        try:
            # Report busy if any GPU is processing (includes download phase)
            with self._busy_lock:
                any_busy = len(self._busy_gpus) > 0
            if any_busy:
                status = "busy"
            else:
                gpu_ready = self._check_gpu_ready()
                status = "online" if gpu_ready else "busy"
            new_logs = log_buffer.get_new_lines()
            cpu = get_cpu_stats()
            r = self._api(
                "post",
                f"/api/nodes/{self.node_id}/heartbeat",
                json={
                    "vram_free_gb": 0,
                    "status": status,
                    "logs": new_logs,
                    "cpu_stats": cpu.to_dict(),
                },
            )
            if r.status_code == 410:
                # Node was explicitly removed via UI — shut down gracefully
                logger.warning("Node was removed by an administrator — shutting down")
                self._dismissed = True
                return False
            if r.status_code == 404:
                # Server restarted and lost our registration — re-register
                logger.info("Server lost registration, re-registering...")
                self._register()
                return True
            return r.status_code == 200
        except Exception:
            return False

    def _poll_job(self) -> dict | None:
        """Poll for the next available job. Skips if GPU is busy."""
        if not self._check_gpu_ready():
            return None
        try:
            r = self._api("get", f"/api/nodes/{self.node_id}/next-job")
            if r.status_code == 404:
                # Not registered — heartbeat will handle re-registration
                return None
            r.raise_for_status()
            data = r.json()
            return data.get("job")
        except Exception as e:
            logger.debug(f"Poll failed: {e}")
            return None

    def _report_progress(self, job_id: str, current: int, total: int) -> bool:
        """Report progress. Returns False if the job was cancelled server-side."""
        if self.tray and current > 0:
            self.tray.set_progress(job_id, current, total)
        try:
            params = {"job_id": job_id, "current": current, "total": total}
            r = self._api("post", f"/api/nodes/{self.node_id}/job-progress", params=params)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "cancelled":
                    logger.warning(f"Job {job_id} was cancelled by server")
                    return False
            return True
        except Exception:
            pass

    def _is_cancelled(self, job_id: str) -> bool:
        """Quick check if a job was cancelled server-side (reuses progress endpoint)."""
        try:
            params = {"job_id": job_id, "current": 0, "total": 0}
            r = self._api("post", f"/api/nodes/{self.node_id}/job-progress", params=params)
            if r.status_code == 200:
                return r.json().get("status") == "cancelled"
        except Exception:
            pass
        return False

    def _report_result(
        self,
        job_id: str,
        status: str,
        error: str | None = None,
        download_mbps: float = 0.0,
        upload_mbps: float = 0.0,
    ) -> None:
        if self.tray:
            if status == "completed":
                self.tray.job_completed(job_id, 0)
            elif status == "failed":
                self.tray.job_failed(job_id, error or "Unknown error")
            else:
                self.tray.set_status("idle")
        try:
            payload: dict = {"job_id": job_id, "status": status, "error_message": error}
            if download_mbps > 0:
                payload["download_mbps"] = round(download_mbps, 2)
            if upload_mbps > 0:
                payload["upload_mbps"] = round(upload_mbps, 2)
            self._api("post", f"/api/nodes/{self.node_id}/job-result", json=payload)
        except Exception as e:
            logger.error(f"Failed to report result for {job_id}: {e}")

    def _process_job_on_gpu(self, job_data: dict, gpu_index: int) -> None:
        """Process a job on a specific GPU, then release the GPU slot."""
        from .file_transfer import TransferCancelled

        try:
            self._process_job(job_data, gpu_index)
        except TransferCancelled:
            logger.info(f"Job {job_data['id']} cancelled during file transfer")
            self._report_result(job_data["id"], "cancelled")
        except Exception as e:
            logger.exception(f"Job processing failed: {e}")
            self._report_result(job_data["id"], "failed", str(e))
        finally:
            with self._busy_lock:
                self._busy_gpus.discard(gpu_index)

    def _process_job(self, job_data: dict, gpu_index: int = 0) -> None:
        """Process a job -- run inference using a GPU subprocess or in-process."""
        from .file_transfer import TransferStats

        job_id = job_data["id"]
        clip_name = job_data["clip_name"]
        use_shared = job_data.get("use_shared_storage", False)

        logger.info(f"Processing job {job_id}: {job_data['job_type']} for '{clip_name}' on GPU {gpu_index}")

        # Set job context for org-scoped file resolution (survives server restarts)
        self.file_transfer.set_job_id(job_id)

        dl_stats = TransferStats()
        ul_stats = TransferStats()

        if use_shared:
            clips_dir = str(Path(job_data.get("shared_clip_root", "")).parent)
        else:
            clips_dir, dl_stats = self._download_job_files(job_data)
            if dl_stats.mbps > 0:
                logger.info(f"Download complete: {dl_stats.mbps:.1f} MB/s")
            # Downloaded files ARE the frame range -- strip it so inference
            # processes all local files instead of re-indexing into the subset
            if job_data.get("params", {}).get("frame_range"):
                job_data = {**job_data, "params": {**job_data["params"], "frame_range": None}}

        if len(self._gpu_indices) == 1:
            self._run_single_gpu(job_data, clips_dir)
        else:
            self._run_subprocess_gpu(job_data, clips_dir, gpu_index)

        # Check cancellation before uploading (avoid wasting bandwidth)
        if self._is_cancelled(job_id):
            logger.info(f"Job {job_id} cancelled before upload -- skipping result upload")
            if not use_shared and clips_dir:
                self._cleanup_temp(clips_dir)
            self._report_result(job_id, "cancelled", download_mbps=dl_stats.mbps)
            return

        # Upload results BEFORE reporting completion
        if not use_shared and clips_dir:
            out_cfg = job_data.get("params", {}).get("output_config", {})
            enabled_outputs = [
                k
                for k, enabled in [
                    ("fg", out_cfg.get("fg_enabled", False)),
                    ("matte", out_cfg.get("matte_enabled", False)),
                    ("comp", out_cfg.get("comp_enabled", True)),
                    ("processed", out_cfg.get("processed_enabled", True)),
                ]
                if enabled
            ] or None
            ul_stats = self._upload_results(
                clip_name,
                clips_dir,
                job_type=job_data.get("job_type", ""),
                job_id=job_id,
                enabled_outputs=enabled_outputs,
            )
            if ul_stats.mbps > 0:
                logger.info(f"Upload complete: {ul_stats.mbps:.1f} MB/s")
            self._cleanup_temp(clips_dir)

        # Only report completed after results are uploaded to the server
        self._report_result(job_id, "completed", download_mbps=dl_stats.mbps, upload_mbps=ul_stats.mbps)

    def _download_job_files(self, job_data: dict) -> tuple:
        """Download input files for a job. Returns (clips_dir, TransferStats).

        Downloads multiple passes in parallel to reduce transfer time.
        """
        from .file_transfer import TransferStats

        clip_name = job_data["clip_name"]
        job_type = job_data["job_type"]
        params = job_data.get("params", {})

        # Only download frames within the shard's range
        frame_range = params.get("frame_range")
        fr = tuple(frame_range) if frame_range else None

        # Use configured temp dir (tmpfs in hardened mode) or system default
        from . import config

        temp_root = config.TEMP_DIR or None
        base_dir = tempfile.mkdtemp(prefix=f"ck-node-{clip_name}-", dir=temp_root)
        clip_dir = os.path.join(base_dir, clip_name)

        # Build list of passes to download
        passes: list[tuple[str, tuple[int, int] | None]] = []
        if job_type == "inference":
            passes = [("input", fr), ("alpha", fr)]
        elif job_type == "gvm_alpha":
            passes = [("input", fr)]
        elif job_type == "videomama_alpha":
            passes = [("input", None), ("mask", None)]

        job_id = job_data["id"]

        def cancel_fn() -> bool:
            return self._is_cancelled(job_id)

        # Collect per-thread stats
        thread_stats: list[TransferStats] = []
        stats_lock = threading.Lock()

        def _download_with_stats(pass_name: str, pass_fr: tuple[int, int] | None) -> None:
            _count, stats = self.file_transfer.download_pass(
                clip_name, pass_name, clip_dir, frame_range=pass_fr, is_cancelled=cancel_fn
            )
            with stats_lock:
                thread_stats.append(stats)

        # Download passes in parallel -- use wall-clock time for effective throughput
        import time as _time

        t0 = _time.monotonic()
        if len(passes) > 1:
            threads = []
            for pass_name, pass_fr in passes:
                t = threading.Thread(target=_download_with_stats, args=(pass_name, pass_fr), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        elif passes:
            _download_with_stats(passes[0][0], passes[0][1])
        wall_elapsed = _time.monotonic() - t0

        # Combine: sum bytes across threads, use wall-clock for elapsed
        total_bytes = sum(s.bytes_transferred for s in thread_stats)
        combined = TransferStats(bytes_transferred=total_bytes, elapsed_seconds=wall_elapsed)

        return base_dir, combined

    def _upload_results(
        self,
        clip_name: str,
        clips_dir: str,
        job_type: str = "",
        job_id: str = "",
        enabled_outputs: list[str] | None = None,
    ):
        """Upload output files back to the main machine. Checks cancellation between passes."""
        from .file_transfer import TransferStats

        clip_dir = os.path.join(clips_dir, clip_name)

        # Inference outputs -- only upload enabled passes
        all_passes = {
            "fg": os.path.join(clip_dir, "Output", "FG"),
            "matte": os.path.join(clip_dir, "Output", "Matte"),
            "comp": os.path.join(clip_dir, "Output", "Comp"),
            "processed": os.path.join(clip_dir, "Output", "Processed"),
        }

        if enabled_outputs:
            output_map = {k: v for k, v in all_passes.items() if k in enabled_outputs}
            skipped = [k for k in all_passes if k not in enabled_outputs]
            if skipped:
                logger.info(f"Skipping disabled output passes: {', '.join(skipped)}")
        else:
            # Default: skip comp for remote nodes (server generates previews on demand)
            output_map = {k: v for k, v in all_passes.items() if k != "comp"}
            logger.info("Skipping comp pass upload (server generates previews on demand)")

        # Only upload alpha hints for jobs that generate them (GVM/VideoMaMa)
        if job_type in ("gvm_alpha", "videomama_alpha"):
            output_map["alpha"] = os.path.join(clip_dir, "AlphaHint")

        cancel_fn = (lambda: self._is_cancelled(job_id)) if job_id else None

        total_bytes = 0
        total_elapsed = 0.0
        for pass_name, dir_path in output_map.items():
            if os.path.isdir(dir_path):
                _count, stats = self.file_transfer.upload_directory(
                    clip_name, pass_name, dir_path, is_cancelled=cancel_fn
                )
                total_bytes += stats.bytes_transferred
                total_elapsed += stats.elapsed_seconds

        return TransferStats(bytes_transferred=total_bytes, elapsed_seconds=total_elapsed)

    def _cleanup_temp(self, clips_dir: str) -> None:
        """Remove temp directory after upload. Only deletes dirs we created."""
        from . import config

        allowed_roots = [tempfile.gettempdir()]
        if config.TEMP_DIR:
            allowed_roots.append(config.TEMP_DIR)
        if not any(clips_dir.startswith(r) for r in allowed_roots):
            return  # safety: don't delete non-temp paths
        try:
            shutil.rmtree(clips_dir)
            logger.debug(f"Cleaned up temp dir: {clips_dir}")
        except Exception as e:
            logger.warning(f"Failed to clean temp dir {clips_dir}: {e}")

    def _run_single_gpu(self, job_data: dict, clips_dir: str) -> None:
        """Run job in-process on a single GPU."""
        job_id = job_data["id"]

        # Set CUDA_VISIBLE_DEVICES for this process
        os.environ["CUDA_VISIBLE_DEVICES"] = str(self._gpu_indices[0])

        from backend.job_queue import GPUJob, JobType
        from backend.service import CorridorKeyService, InferenceParams, OutputConfig

        service = CorridorKeyService()
        service.detect_device()

        job_type = JobType(job_data["job_type"])
        clip_name = job_data["clip_name"]
        params = job_data.get("params", {})

        clips = service.scan_clips(clips_dir)
        clip = next((c for c in clips if c.name == clip_name), None)
        if clip is None:
            # Raise so _process_job_on_gpu catches it and reports failed.
            # Reporting failed inline and returning normally would fall
            # through to _process_job's upload + "completed" path and
            # clobber the failed state with a bogus "completed 0 frames".
            raise RuntimeError(f"Clip '{clip_name}' not found in {clips_dir}")

        job = GPUJob(job_type=job_type, clip_name=clip_name, params=params)
        job.id = job_id

        def on_progress(cn: str, current: int, total: int) -> None:
            if not self._report_progress(job_id, current, total):
                # Server cancelled this job — request_cancel sets
                # _cancel_requested which service.py checks per-frame
                job.request_cancel()

        def on_warning(message: str) -> None:
            logger.warning(f"Job {job_id}: {message}")

        try:
            if job_type == JobType.INFERENCE:
                inf_params = InferenceParams.from_dict(params.get("inference_params", {}))
                output_config = OutputConfig.from_dict(params.get("output_config", {}))
                frame_range = params.get("frame_range")
                service.run_inference(
                    clip,
                    inf_params,
                    job=job,
                    on_progress=on_progress,
                    on_warning=on_warning,
                    output_config=output_config,
                    frame_range=tuple(frame_range) if frame_range else None,
                )
            elif job_type == JobType.GVM_ALPHA:
                gvm_frame_range = params.get("frame_range")
                service.run_gvm(
                    clip,
                    job=job,
                    on_progress=on_progress,
                    on_warning=on_warning,
                    frame_range=tuple(gvm_frame_range) if gvm_frame_range else None,
                )
            elif job_type == JobType.VIDEOMAMA_ALPHA:
                chunk_size = params.get("chunk_size", 50)
                service.run_videomama(
                    clip, job=job, on_progress=on_progress, on_warning=on_warning, chunk_size=chunk_size
                )

            # Don't report completed here — _process_job reports after upload
        except Exception:
            # Log with job context then re-raise so _process_job_on_gpu
            # catches it and reports failed. Reporting "failed" inline
            # and falling through would let _process_job overwrite the
            # failed state with "completed" after running the upload
            # step on zero output — the exact "COMPLETED 0 frames 0 fps"
            # bug users saw when GVM crashed on PyInstaller metadata
            # lookups like "No package metadata was found for imageio".
            logger.exception(f"Job {job_id} failed during GPU execution")
            raise
        finally:
            service.unload_engines()

    def _run_subprocess_gpu(self, job_data: dict, clips_dir: str, gpu_index: int) -> None:
        """Run job in a subprocess on a specific GPU."""
        from web.shared.gpu_subprocess import gpu_worker_main

        task_queue = _mp.Queue()
        result_queue = _mp.Queue()

        proc = _mp.Process(target=gpu_worker_main, args=(gpu_index, task_queue, result_queue), daemon=True)
        proc.start()

        job_id = job_data["id"]
        failure: str | None = None

        try:
            # Wait for ready
            msg = result_queue.get(timeout=60)
            if msg.get("status") != "ready":
                failure = "GPU worker failed to start"
            else:
                # Send the job (already a dict, just need to wrap it)
                task_queue.put({"action": "run", "job": job_data, "clips_dir": clips_dir})

                while True:
                    try:
                        msg = result_queue.get(timeout=600)
                    except Exception:
                        failure = "Timed out waiting for GPU worker"
                        break

                    status = msg.get("status")
                    if status == "progress":
                        self._report_progress(job_id, msg.get("current", 0), msg.get("total", 0))
                    elif status == "completed":
                        # Don't report completed here — _process_job reports after upload
                        break
                    elif status == "failed":
                        failure = msg.get("error", "Unknown error")
                        break
        finally:
            task_queue.put({"action": "stop"})
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()

        if failure is not None:
            # Raise so _process_job_on_gpu catches it and reports failed.
            # Reporting inline here and returning normally would fall
            # through to _process_job's upload + "completed" path and
            # clobber the failure with "completed 0 frames".
            raise RuntimeError(failure)

    def _check_gpu_compatibility(self) -> bool:
        """Verify selected GPUs meet the minimum compute capability for this torch build.

        If any selected GPU is incompatible, log a clear error and return False.
        The caller should exit instead of proceeding to register — otherwise the
        node would accept jobs it can't run, silently fail them, and keep polling.
        """
        gpus = enumerate_gpus()
        selected = [g for g in gpus if g.index in self._gpu_indices]
        if not selected:
            # No GPUs detected or selected — fall through to normal flow,
            # torch / CPU-only mode will handle it.
            return True
        incompatible: list[str] = []
        for g in selected:
            ok, reason = check_gpu_torch_compat(g)
            if not ok:
                incompatible.append(reason)
        if incompatible:
            logger.error("Node GPU hardware is incompatible with this node build:")
            for msg in incompatible:
                logger.error(f"  - {msg}")
            logger.error(
                "See https://corridorkey.cloud/nodes/setup for supported hardware. "
                "The agent will exit instead of accepting jobs it cannot run."
            )
            return False
        return True

    def run(self) -> None:
        """Main loop — sync weights, register, then poll for jobs."""
        logger.info(f"CorridorKey Node Agent starting: {self.name} ({self.node_id})")
        logger.info(f"Main server: {self.main_url}")
        logger.info(f"GPUs: {self._gpu_indices}")
        if self.shared_storage:
            logger.info(f"Shared storage: {self.shared_storage}")

        # Hard gate: refuse to run if selected GPUs are below the torch build's
        # minimum compute capability. Without this the node accepts jobs and
        # silently fails them on every kernel launch (CRKY-188).
        if not self._check_gpu_compatibility():
            if self.tray:
                self.tray.set_status("error")
            return

        # Sync weights from main server before doing anything else
        logger.info("Checking model weights...")
        try:
            sync_weights(self.main_url)
        except Exception as e:
            logger.warning(f"Weight sync failed (will try to proceed): {e}")

        # Pre-warm model into VRAM
        if config.PREWARM:
            self._prewarm()

        # Register (retry until success)
        while not self._stop.is_set():
            if self._register():
                break
            logger.info(f"Retrying registration in {self.heartbeat_interval}s...")
            self._stop.wait(self.heartbeat_interval)

        # Start heartbeat thread
        hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat")
        hb_thread.start()

        # Poll loop with backoff: start fast, slow down when idle, reset on job found
        _idle_polls = 0
        _MAX_IDLE_INTERVAL = 10.0  # max seconds between polls when idle
        logger.info(f"Polling for jobs... ({len(self._gpu_indices)} GPU(s) available)")
        while not self._stop.is_set() and not self._dismissed:
            # Tray pause — skip polling but keep heartbeat alive
            if self.tray and self.tray.paused:
                self._stop.wait(self.poll_interval)
                continue

            # Check if we have an idle GPU
            with self._busy_lock:
                idle_gpus = [g for g in self._gpu_indices if g not in self._busy_gpus]

            if not idle_gpus:
                self._stop.wait(self.poll_interval)
                continue

            job_data = self._poll_job()
            if job_data:
                _idle_polls = 0  # reset backoff
                gpu_index = idle_gpus[0]
                with self._busy_lock:
                    self._busy_gpus.add(gpu_index)
                # Process in a thread so we can accept more jobs on other GPUs
                t = threading.Thread(
                    target=self._process_job_on_gpu,
                    args=(job_data, gpu_index),
                    daemon=True,
                    name=f"gpu-job-{gpu_index}",
                )
                t.start()
            else:
                _idle_polls += 1
                # Backoff: 2s → 4s → 6s → 8s → 10s (cap)
                wait = min(self.poll_interval * _idle_polls, _MAX_IDLE_INTERVAL)
                self._stop.wait(wait)

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set() and not self._dismissed:
            self._stop.wait(self.heartbeat_interval)
            if not self._stop.is_set() and not self._dismissed:
                self._heartbeat()

    def stop(self) -> None:
        self._stop.set()
        # Don't unregister — let the heartbeat timeout mark us offline.
        # The user can manually remove the node from the UI if desired.
        logger.info("Node agent stopped")
