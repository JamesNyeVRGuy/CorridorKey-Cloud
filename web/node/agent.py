"""Node agent — registers with the main machine, polls for jobs, processes them.

Supports multi-GPU: spawns one subprocess per GPU using the shared
gpu_subprocess module. Single-GPU mode runs inference in-process.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import threading
from multiprocessing import Process, Queue
from pathlib import Path

import httpx

from device_utils import enumerate_gpus

from . import config
from .file_transfer import FileTransfer
from .weight_sync import sync_weights

logger = logging.getLogger(__name__)


class NodeAgent:
    """Lightweight agent that connects to the main CorridorKey server."""

    def __init__(self):
        self.node_id = config.NODE_ID
        self.name = config.NODE_NAME
        self.main_url = config.MAIN_URL.rstrip("/")
        self.shared_storage = config.SHARED_STORAGE or None
        self.poll_interval = config.POLL_INTERVAL
        self.heartbeat_interval = config.HEARTBEAT_INTERVAL
        self.file_transfer = FileTransfer(self.main_url, self.node_id)

        self._stop = threading.Event()
        self._gpu_indices = self._resolve_gpus()
        self._gpu_workers: dict[int, Process] = {}
        self._result_queue: Queue = Queue()

    def _resolve_gpus(self) -> list[int]:
        """Determine which GPUs to use based on config."""
        if config.NODE_GPUS == "auto":
            gpus = enumerate_gpus()
            return [g.index for g in gpus] if gpus else [0]
        return [int(x.strip()) for x in config.NODE_GPUS.split(",") if x.strip()]

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
        with httpx.Client(timeout=30) as client:
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
        }

        try:
            r = self._api("post", "/api/nodes/register", json=payload)
            r.raise_for_status()
            logger.info(f"Registered as '{self.name}' ({self.node_id}) with {len(gpu_slots)} GPU(s)")
            return True
        except Exception as e:
            logger.error(f"Registration failed: {e}")
            return False

    def _heartbeat(self) -> bool:
        try:
            r = self._api("post", f"/api/nodes/{self.node_id}/heartbeat", json={"vram_free_gb": 0, "status": "online"})
            return r.status_code == 200
        except Exception:
            return False

    def _poll_job(self) -> dict | None:
        """Poll for the next available job."""
        try:
            r = self._api("get", f"/api/nodes/{self.node_id}/next-job")
            r.raise_for_status()
            data = r.json()
            return data.get("job")
        except Exception as e:
            logger.debug(f"Poll failed: {e}")
            return None

    def _report_progress(self, job_id: str, current: int, total: int) -> None:
        try:
            params = {"job_id": job_id, "current": current, "total": total}
            self._api("post", f"/api/nodes/{self.node_id}/job-progress", params=params)
        except Exception:
            pass

    def _report_result(self, job_id: str, status: str, error: str | None = None) -> None:
        try:
            payload = {"job_id": job_id, "status": status, "error_message": error}
            self._api("post", f"/api/nodes/{self.node_id}/job-result", json=payload)
        except Exception as e:
            logger.error(f"Failed to report result for {job_id}: {e}")

    def _process_job(self, job_data: dict) -> None:
        """Process a job — run inference using a GPU subprocess or in-process."""
        job_id = job_data["id"]
        clip_name = job_data["clip_name"]
        use_shared = job_data.get("use_shared_storage", False)

        logger.info(f"Processing job {job_id}: {job_data['job_type']} for '{clip_name}'")

        if use_shared:
            clips_dir = str(Path(job_data.get("shared_clip_root", "")).parent)
        else:
            # Download files to a temp directory
            clips_dir = self._download_job_files(job_data)

        if len(self._gpu_indices) == 1:
            self._run_single_gpu(job_data, clips_dir)
        else:
            # Multi-GPU: use subprocess
            self._run_subprocess_gpu(job_data, clips_dir, self._gpu_indices[0])

        # Upload results if not using shared storage
        if not use_shared and clips_dir:
            self._upload_results(clip_name, clips_dir)

    def _download_job_files(self, job_data: dict) -> str:
        """Download input files for a job. Returns the clips_dir path."""
        clip_name = job_data["clip_name"]
        job_type = job_data["job_type"]

        base_dir = tempfile.mkdtemp(prefix=f"ck-node-{clip_name}-")
        clip_dir = os.path.join(base_dir, clip_name)

        if job_type == "inference":
            # Need input frames and alpha hints
            self.file_transfer.download_pass(clip_name, "input", os.path.join(clip_dir, "Frames", "Input"))
            self.file_transfer.download_pass(clip_name, "alpha", os.path.join(clip_dir, "AlphaHint"))
        elif job_type == "gvm_alpha":
            self.file_transfer.download_pass(clip_name, "input", os.path.join(clip_dir, "Frames", "Input"))
        elif job_type == "videomama_alpha":
            self.file_transfer.download_pass(clip_name, "input", os.path.join(clip_dir, "Frames", "Input"))
            self.file_transfer.download_pass(clip_name, "mask", os.path.join(clip_dir, "VideoMamaMaskHint"))

        return base_dir

    def _upload_results(self, clip_name: str, clips_dir: str) -> None:
        """Upload output files back to the main machine."""
        clip_dir = os.path.join(clips_dir, clip_name)

        output_map = {
            "fg": os.path.join(clip_dir, "Output", "FG"),
            "matte": os.path.join(clip_dir, "Output", "Matte"),
            "comp": os.path.join(clip_dir, "Output", "Comp"),
            "processed": os.path.join(clip_dir, "Output", "Processed"),
            "alpha": os.path.join(clip_dir, "AlphaHint"),
        }

        for pass_name, dir_path in output_map.items():
            if os.path.isdir(dir_path):
                self.file_transfer.upload_directory(clip_name, pass_name, dir_path)

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
            self._report_result(job_id, "failed", f"Clip '{clip_name}' not found in {clips_dir}")
            return

        job = GPUJob(job_type=job_type, clip_name=clip_name, params=params)
        job.id = job_id

        def on_progress(cn: str, current: int, total: int) -> None:
            self._report_progress(job_id, current, total)

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
                service.run_gvm(clip, job=job, on_progress=on_progress, on_warning=on_warning)
            elif job_type == JobType.VIDEOMAMA_ALPHA:
                chunk_size = params.get("chunk_size", 50)
                service.run_videomama(
                    clip, job=job, on_progress=on_progress, on_warning=on_warning, chunk_size=chunk_size
                )

            self._report_result(job_id, "completed")
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
                self._report_result(job_id, "completed")
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
        logger.info("Polling for jobs...")
        while not self._stop.is_set():
            job_data = self._poll_job()
            if job_data:
                try:
                    self._process_job(job_data)
                except Exception as e:
                    logger.exception(f"Job processing failed: {e}")
                    self._report_result(job_data["id"], "failed", str(e))
            else:
                self._stop.wait(self.poll_interval)

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.heartbeat_interval)
            if not self._stop.is_set():
                self._heartbeat()

    def stop(self) -> None:
        self._stop.set()
        # Don't unregister — let the heartbeat timeout mark us offline.
        # The user can manually remove the node from the UI if desired.
        logger.info("Node agent stopped")
