"""Node registry — tracks remote worker machines and dispatches jobs to them."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class NodeSchedule:
    """Active hours schedule for a node."""

    enabled: bool = False
    start: str = "00:00"  # HH:MM (24h)
    end: str = "23:59"  # HH:MM (24h)

    @property
    def is_active_now(self) -> bool:
        """Check if the current time is within the active window."""
        if not self.enabled:
            return True  # no schedule = always active

        now = datetime.now().strftime("%H:%M")
        if self.start <= self.end:
            # Same-day window: e.g. 09:00-17:00
            return self.start <= now <= self.end
        else:
            # Overnight window: e.g. 20:00-08:00
            return now >= self.start or now <= self.end

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "start": self.start,
            "end": self.end,
            "is_active_now": self.is_active_now,
        }


@dataclass
class GPUSlot:
    """A single GPU on a node."""

    index: int
    name: str
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0
    status: str = "idle"  # idle, busy
    current_job_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "vram_total_gb": round(self.vram_total_gb, 1),
            "vram_free_gb": round(self.vram_free_gb, 1),
            "status": self.status,
            "current_job_id": self.current_job_id,
        }


@dataclass
class NodeInfo:
    """A registered remote worker node."""

    node_id: str
    name: str
    host: str  # IP/hostname the node reported
    gpus: list[GPUSlot] = field(default_factory=list)
    # Legacy single-GPU fields (used when gpus list is empty)
    gpu_name: str = ""
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0
    status: str = "online"  # online, busy, offline
    current_job_id: str | None = None
    last_heartbeat: float = field(default_factory=time.time)
    capabilities: list[str] = field(default_factory=list)  # ["cuda", "mlx", "cpu"]
    shared_storage: str | None = None  # path if node has shared storage mounted
    paused: bool = False
    schedule: NodeSchedule = field(default_factory=NodeSchedule)

    @property
    def is_alive(self) -> bool:
        return time.time() - self.last_heartbeat < 30  # 30s timeout

    @property
    def can_accept_jobs(self) -> bool:
        """True if the node is alive, not paused, and within its schedule."""
        return self.is_alive and not self.paused and self.schedule.is_active_now

    @property
    def gpu_count(self) -> int:
        return len(self.gpus) if self.gpus else (1 if self.gpu_name else 0)

    @property
    def has_idle_gpu(self) -> bool:
        if self.gpus:
            return any(g.status == "idle" for g in self.gpus)
        return self.status == "online"

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "host": self.host,
            "gpus": [g.to_dict() for g in self.gpus],
            "gpu_name": self.gpu_name,
            "vram_total_gb": round(self.vram_total_gb, 1),
            "vram_free_gb": round(self.vram_free_gb, 1),
            "status": self.status if self.is_alive else "offline",
            "current_job_id": self.current_job_id,
            "last_heartbeat": self.last_heartbeat,
            "capabilities": self.capabilities,
            "shared_storage": self.shared_storage,
            "paused": self.paused,
            "schedule": self.schedule.to_dict(),
        }


class NodeRegistry:
    """Thread-safe registry of remote worker nodes."""

    def __init__(self):
        self._nodes: dict[str, NodeInfo] = {}
        self._lock = threading.Lock()

    def register(self, info: NodeInfo) -> None:
        with self._lock:
            existing = self._nodes.get(info.node_id)
            if existing:
                existing.name = info.name
                existing.host = info.host
                existing.gpus = info.gpus
                existing.gpu_name = info.gpu_name
                existing.vram_total_gb = info.vram_total_gb
                existing.vram_free_gb = info.vram_free_gb
                existing.capabilities = info.capabilities
                existing.shared_storage = info.shared_storage
                existing.status = "online"
                existing.last_heartbeat = time.time()
                # Preserve paused and schedule on re-register (set from UI)
                logger.info(f"Node re-registered: {info.name} ({info.node_id})")
            else:
                info.last_heartbeat = time.time()
                self._nodes[info.node_id] = info
                gpu_desc = ", ".join(g.name for g in info.gpus) if info.gpus else info.gpu_name
                logger.info(f"Node registered: {info.name} ({info.node_id}) — {gpu_desc}")

    def heartbeat(self, node_id: str, vram_free_gb: float = 0.0, status: str = "online") -> bool:
        """Update heartbeat. Returns False if node not found."""
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return False
            node.last_heartbeat = time.time()
            node.vram_free_gb = vram_free_gb
            node.status = status
            return True

    def unregister(self, node_id: str) -> None:
        with self._lock:
            if node_id in self._nodes:
                logger.info(f"Node unregistered: {self._nodes[node_id].name} ({node_id})")
                del self._nodes[node_id]

    def set_busy(self, node_id: str, job_id: str) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if node:
                node.status = "busy"
                node.current_job_id = job_id

    def set_idle(self, node_id: str) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if node:
                node.status = "online"
                node.current_job_id = None

    def get_available_node(self, min_vram_gb: float = 0.0) -> NodeInfo | None:
        """Find an available node with enough VRAM."""
        with self._lock:
            for node in self._nodes.values():
                if node.is_alive and node.status == "online":
                    if min_vram_gb <= 0 or node.vram_free_gb >= min_vram_gb:
                        return node
            return None

    def get_node(self, node_id: str) -> NodeInfo | None:
        with self._lock:
            return self._nodes.get(node_id)

    def list_nodes(self) -> list[NodeInfo]:
        with self._lock:
            return list(self._nodes.values())

    @property
    def online_count(self) -> int:
        with self._lock:
            return sum(1 for n in self._nodes.values() if n.is_alive)


# Global singleton
registry = NodeRegistry()
