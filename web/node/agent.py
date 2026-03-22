"""Node agent — registers with the main machine, polls for jobs, processes them.

Supports multi-GPU: spawns one subprocess per GPU using the shared
gpu_subprocess module. Single-GPU mode runs inference in-process.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import tempfile
import threading
from multiprocessing import Process, Queue
from pathlib import Path

import httpx

from device_utils import check_gpu_available, enumerate_gpus, get_cpu_stats

from . import config
from .file_transfer import FileTransfer
from .log_buffer import buffer as log_buffer
from .weight_sync import sync_weights

logger = logging.getLogger(__name__)


def _get_local_version() -> str:
    """Detect version from git at runtime (dev mode)."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


class NodeAgent:
    """Lightweight agent that connects to the main CorridorKey server."""

    def __init__(self):
        self.node_id = config.NODE_ID
        self.name = config.NODE_NAME
        self.main_url = config.MAIN_URL.rstrip("/")
        self.shared_storage = config.SHARED_STORAGE or None
        self.poll_interval = config.POLL_INTERVAL
        self.heartbeat_interval = config.HEARTBEAT_INTERVAL
        self.file_transfer = FileTransfer(self.main_url, self.node_id, auth_token=config.AUTH_TOKEN)

        self._stop = threading.Event()
        self._dismissed = False  # Set when server returns 410 (explicitly removed)
        self._gpu_indices = self._resolve_gpus()
        self._busy_gpus: set[int] = set()  # GPU indices currently processing
        self._busy_lock = threading.Lock()

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
            svc._get_engine()
            elapsed = _time.monotonic() - t0
            logger.info(f"Model pre-warmed in {elapsed:.1f}s")
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
            "shared_storage": self.shared_storage,
            "accepted_types": [t.strip() for t in config.ACCEPTED_TYPES.split(",") if t.strip()],
            "security": {
                "running_as_root": os.getuid() == 0,
                "hardened": os.environ.get("CK_NODE_HARDENED", "").strip().lower() in ("true", "1"),
                "uid": os.getuid(),
                "read_only_fs": not os.access("/", os.W_OK),
                "agent_version": os.environ.get("CK_BUILD_COMMIT", "").strip() or _get_local_version(),
            },
        }

        try:
            r = self._api("post", "/api/nodes/register", json=payload)
            r.raise_for_status()
            data = r.json()
            logger.info(f"Registered as '{self.name}' ({self.node_id}) with {len(gpu_slots)} GPU(s)")
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

    def _report_result(self, job_id: str, status: str, error: str | None = None) -> None:
        try:
            payload = {"job_id": job_id, "status": status, "error_message": error}
            self._api("post", f"/api/nodes/{self.node_id}/job-result", json=payload)
        except Exception as e:
            logger.error(f"Failed to report result for {job_id}: {e}")

    def _process_job_on_gpu(self, job_data: dict, gpu_index: int) -> None:
        """Process a job on a specific GPU, then release the GPU slot."""
        try:
            self._process_job(job_data, gpu_index)
        except Exception as e:
            logger.exception(f"Job processing failed: {e}")
            self._report_result(job_data["id"], "failed", str(e))
        finally:
            with self._busy_lock:
                self._busy_gpus.discard(gpu_index)

    def _process_job(self, job_data: dict, gpu_index: int = 0) -> None:
        """Process a job — run inference using a GPU subprocess or in-process."""
        job_id = job_data["id"]
        clip_name = job_data["clip_name"]
        use_shared = job_data.get("use_shared_storage", False)

        logger.info(f"Processing job {job_id}: {job_data['job_type']} for '{clip_name}' on GPU {gpu_index}")

        if use_shared:
            clips_dir = str(Path(job_data.get("shared_clip_root", "")).parent)
        else:
            clips_dir = self._download_job_files(job_data)
            # Downloaded files ARE the frame range — strip it so inference
            # processes all local files instead of re-indexing into the subset
            if job_data.get("params", {}).get("frame_range"):
                job_data = {**job_data, "params": {**job_data["params"], "frame_range": None}}

        if len(self._gpu_indices) == 1:
            self._run_single_gpu(job_data, clips_dir)
        else:
            self._run_subprocess_gpu(job_data, clips_dir, gpu_index)

        # Upload results BEFORE reporting completion
        if not use_shared and clips_dir:
            self._upload_results(clip_name, clips_dir, job_type=job_data.get("job_type", ""))
            self._cleanup_temp(clips_dir)

        # Only report completed after results are uploaded to the server
        self._report_result(job_id, "completed")

    def _download_job_files(self, job_data: dict) -> str:
        """Download input files for a job. Returns the clips_dir path.

        Downloads multiple passes in parallel to reduce transfer time.
        """
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

        # Download passes in parallel
        if len(passes) > 1:
            threads = []
            for pass_name, pass_fr in passes:
                t = threading.Thread(
                    target=self.file_transfer.download_pass,
                    args=(clip_name, pass_name, clip_dir),
                    kwargs={"frame_range": pass_fr},
                    daemon=True,
                )
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        elif passes:
            self.file_transfer.download_pass(clip_name, passes[0][0], clip_dir, frame_range=passes[0][1])

        return base_dir

    def _upload_results(self, clip_name: str, clips_dir: str, job_type: str = "") -> None:
        """Upload output files back to the main machine."""
        clip_dir = os.path.join(clips_dir, clip_name)

        # Inference outputs
        output_map = {
            "fg": os.path.join(clip_dir, "Output", "FG"),
            "matte": os.path.join(clip_dir, "Output", "Matte"),
            "comp": os.path.join(clip_dir, "Output", "Comp"),
            "processed": os.path.join(clip_dir, "Output", "Processed"),
        }
        # Only upload alpha hints for jobs that generate them (GVM/VideoMaMa)
        if job_type in ("gvm_alpha", "videomama_alpha"):
            output_map["alpha"] = os.path.join(clip_dir, "AlphaHint")

        for pass_name, dir_path in output_map.items():
            if os.path.isdir(dir_path):
                self.file_transfer.upload_directory(clip_name, pass_name, dir_path)

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

        from backend.job_queue import GPUJob, JobStatus, JobType
        from backend.service import CorridorKeyService, InferenceParams, OutputConfig

        service = CorridorKeyService()
        service.detect_device()

        job_type = JobType(job_data["job_type"])
        clip_name = job_data["clip_name"]
        params = job_data.get("params", {})

        clips = service.scan_clips(clips_dir)
        clip = next((c for c in clips if c.name == clip_name), None)
        if clip is None:
            self._report_result(job_id, "failed", f"Clip '{clip_name}' not found in {clips_dir}")
            return

        job = GPUJob(job_type=job_type, clip_name=clip_name, params=params)
        job.id = job_id

        def on_progress(cn: str, current: int, total: int) -> None:
            if not self._report_progress(job_id, current, total):
                # Server cancelled this job — set the cancel flag so
                # the service layer's cancel check picks it up
                job.status = JobStatus.CANCELLED

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
                    clip, job=job, on_progress=on_progress, on_warning=on_warning,
                    frame_range=tuple(gvm_frame_range) if gvm_frame_range else None,
                )
            elif job_type == JobType.VIDEOMAMA_ALPHA:
                chunk_size = params.get("chunk_size", 50)
                service.run_videomama(
                    clip, job=job, on_progress=on_progress, on_warning=on_warning, chunk_size=chunk_size
                )

            # Don't report completed here — _process_job reports after upload
        except Exception as e:
            logger.exception(f"Job {job_id} failed")
            self._report_result(job_id, "failed", str(e))
        finally:
            service.unload_engines()

    def _run_subprocess_gpu(self, job_data: dict, clips_dir: str, gpu_index: int) -> None:
        """Run job in a subprocess on a specific GPU."""
        from web.shared.gpu_subprocess import gpu_worker_main

        task_queue: Queue = Queue()
        result_queue: Queue = Queue()

        proc = Process(target=gpu_worker_main, args=(gpu_index, task_queue, result_queue), daemon=True)
        proc.start()

        # Wait for ready
        msg = result_queue.get(timeout=60)
        if msg.get("status") != "ready":
            self._report_result(job_data["id"], "failed", "GPU worker failed to start")
            proc.terminate()
            return

        # Send the job (already a dict, just need to wrap it)
        task_queue.put({"action": "run", "job": job_data, "clips_dir": clips_dir})

        # Read results
        job_id = job_data["id"]
        while True:
            try:
                msg = result_queue.get(timeout=600)
            except Exception:
                self._report_result(job_id, "failed", "Timed out waiting for GPU worker")
                break

            status = msg.get("status")
            if status == "progress":
                self._report_progress(job_id, msg.get("current", 0), msg.get("total", 0))
            elif status == "completed":
                # Don't report completed here — _process_job reports after upload
                break
            elif status == "failed":
                self._report_result(job_id, "failed", msg.get("error", "Unknown error"))
                break

        task_queue.put({"action": "stop"})
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()

    def run(self) -> None:
        """Main loop — sync weights, register, then poll for jobs."""
        logger.info(f"CorridorKey Node Agent starting: {self.name} ({self.node_id})")
        logger.info(f"Main server: {self.main_url}")
        logger.info(f"GPUs: {self._gpu_indices}")
        if self.shared_storage:
            logger.info(f"Shared storage: {self.shared_storage}")

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

        # Poll loop
        logger.info(f"Polling for jobs... ({len(self._gpu_indices)} GPU(s) available)")
        while not self._stop.is_set() and not self._dismissed:
            # Check if we have an idle GPU
            with self._busy_lock:
                idle_gpus = [g for g in self._gpu_indices if g not in self._busy_gpus]

            if not idle_gpus:
                self._stop.wait(self.poll_interval)
                continue

            job_data = self._poll_job()
            if job_data:
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
                self._stop.wait(self.poll_interval)

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
