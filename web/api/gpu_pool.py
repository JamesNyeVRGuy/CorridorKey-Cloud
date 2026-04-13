"""GPU worker pool — one subprocess per GPU.

Spawns OS processes that each set CUDA_VISIBLE_DEVICES and run
CorridorKeyService independently. OOM on one GPU doesn't crash others.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading

from device_utils import enumerate_gpus
from web.shared.gpu_subprocess import _serialize_job, gpu_worker_main

# Use 'spawn' start method to avoid CUDA re-initialization errors on Linux/Docker.
# fork() copies the parent's CUDA context, which can't be re-initialized in children.
_mp = multiprocessing.get_context("spawn")

logger = logging.getLogger(__name__)


class GPUWorkerSlot:
    """Tracks a single GPU worker subprocess."""

    def __init__(self, gpu_index: int):
        self.gpu_index = gpu_index
        self.task_queue: _mp.Queue = _mp.Queue()
        self.process: _mp.Process | None = None
        self.busy = False
        self.current_job_id: str | None = None

    def start(self, result_queue: _mp.Queue) -> None:
        self.process = _mp.Process(
            target=gpu_worker_main,
            args=(self.gpu_index, self.task_queue, result_queue),
            daemon=True,
            name=f"gpu-worker-{self.gpu_index}",
        )
        self.process.start()

    def stop(self) -> None:
        if self.process and self.process.is_alive():
            self.task_queue.put({"action": "stop"})
            self.process.join(timeout=10)
            if self.process.is_alive():
                self.process.terminate()


class GPUWorkerPool:
    """Pool of GPU worker subprocesses.

    Usage:
        pool = GPUWorkerPool()
        pool.start()
        pool.submit(job, clips_dir)  # dispatches to next idle GPU
        ...
        pool.stop()
    """

    def __init__(self, gpu_indices: list[int] | None = None):
        """
        Args:
            gpu_indices: Which GPUs to use. None = auto-detect all.
        """
        if gpu_indices is None:
            gpus = enumerate_gpus()
            gpu_indices = [g.index for g in gpus] if gpus else [0]

        self._result_queue: _mp.Queue = _mp.Queue()
        self._slots = [GPUWorkerSlot(i) for i in gpu_indices]
        self._lock = threading.Lock()
        self._result_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._on_progress = None
        self._on_warning = None
        self._on_completed = None
        self._on_failed = None

    @property
    def gpu_count(self) -> int:
        return len(self._slots)

    @property
    def idle_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._slots if not s.busy)

    def set_callbacks(self, on_progress=None, on_warning=None, on_completed=None, on_failed=None):
        self._on_progress = on_progress
        self._on_warning = on_warning
        self._on_completed = on_completed
        self._on_failed = on_failed

    def start(self) -> None:
        logger.info(f"Starting GPU worker pool with {len(self._slots)} GPU(s): {[s.gpu_index for s in self._slots]}")
        for slot in self._slots:
            slot.start(self._result_queue)

        # Wait for all workers to report ready
        ready_count = 0
        while ready_count < len(self._slots):
            msg = self._result_queue.get(timeout=60)
            if msg.get("status") == "ready":
                ready_count += 1
                logger.info(f"GPU worker ready: GPU {msg['gpu_index']} ({msg['device']})")

        # Start result reader thread
        self._result_thread = threading.Thread(target=self._read_results, daemon=True, name="gpu-pool-reader")
        self._result_thread.start()
        logger.info("GPU worker pool started")

    def stop(self) -> None:
        self._stop_event.set()
        for slot in self._slots:
            slot.stop()
        if self._result_thread:
            self._result_thread.join(timeout=5)
        logger.info("GPU worker pool stopped")

    def submit(self, job, clips_dir: str) -> bool:
        """Submit a job to the next idle GPU. Returns False if all GPUs are busy."""
        with self._lock:
            for slot in self._slots:
                if not slot.busy:
                    slot.busy = True
                    slot.current_job_id = job.id
                    slot.task_queue.put(
                        {
                            "action": "run",
                            "job": _serialize_job(job),
                            "clips_dir": clips_dir,
                        }
                    )
                    logger.info(f"Job {job.id} dispatched to GPU {slot.gpu_index}")
                    return True
        return False

    def has_idle_gpu(self) -> bool:
        with self._lock:
            return any(not s.busy for s in self._slots)

    def _read_results(self) -> None:
        """Read results from worker subprocesses."""
        while not self._stop_event.is_set():
            try:
                msg = self._result_queue.get(timeout=0.5)
            except Exception:
                continue

            status = msg.get("status")
            job_id = msg.get("job_id")

            if status == "progress" and self._on_progress:
                self._on_progress(job_id, msg.get("clip_name", ""), msg.get("current", 0), msg.get("total", 0))
            elif status == "warning" and self._on_warning:
                self._on_warning(job_id, msg.get("message", ""))
            elif status == "completed":
                self._mark_idle(job_id)
                if self._on_completed:
                    self._on_completed(job_id, msg.get("clip_name", ""), msg.get("clip_state", ""))
            elif status == "failed":
                self._mark_idle(job_id)
                if self._on_failed:
                    self._on_failed(job_id, msg.get("error", "Unknown error"))

    def _mark_idle(self, job_id: str) -> None:
        with self._lock:
            for slot in self._slots:
                if slot.current_job_id == job_id:
                    slot.busy = False
                    slot.current_job_id = None
                    break
