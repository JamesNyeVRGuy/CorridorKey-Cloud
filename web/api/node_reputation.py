"""Node reputation scoring (CRKY-30).

Tracks per-node reliability metrics and computes a composite reputation
score 0-100. Low-reputation nodes are auto-paused.

Metrics:
- Job success rate: completed / (completed + failed). Weight: 50%
- Average frames/sec: from job duration + frame count. Weight: 20%
- Uptime: heartbeat regularity. Weight: 30%

Stored in the database via storage backend.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Nodes below this score are auto-paused until reviewed
AUTO_PAUSE_THRESHOLD = 40


@dataclass
class NodeReputation:
    """Reputation record for a node."""

    node_id: str
    completed_jobs: int = 0
    failed_jobs: int = 0
    total_frames: int = 0
    total_processing_seconds: float = 0.0
    missed_heartbeats: int = 0
    total_heartbeats: int = 0
    security_warnings: int = 0  # count of security issues detected on registration
    last_updated: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.completed_jobs + self.failed_jobs
        return self.completed_jobs / total if total > 0 else 1.0

    @property
    def avg_fps(self) -> float:
        if self.total_processing_seconds <= 0 or self.total_frames <= 0:
            return 0.0
        return self.total_frames / self.total_processing_seconds

    @property
    def uptime_rate(self) -> float:
        if self.total_heartbeats <= 0:
            return 1.0
        return max(0, 1.0 - (self.missed_heartbeats / self.total_heartbeats))

    @property
    def security_penalty(self) -> float:
        """Penalty for security warnings (0-15 points deducted)."""
        return min(15, self.security_warnings * 5)

    @property
    def score(self) -> int:
        """Composite reputation score 0-100."""
        s = (self.success_rate * 50) + (min(1.0, self.avg_fps / 2.0) * 20) + (self.uptime_rate * 30)
        s -= self.security_penalty
        return max(0, min(100, round(s)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "score": self.score,
            "breakdown": {
                "success": {
                    "value": round(self.success_rate, 3),
                    "weight": 50,
                    "points": round(self.success_rate * 50, 1),
                },
                "speed": {
                    "value": round(self.avg_fps, 2),
                    "weight": 20,
                    "points": round(min(1.0, self.avg_fps / 2.0) * 20, 1),
                },
                "uptime": {
                    "value": round(self.uptime_rate, 3),
                    "weight": 30,
                    "points": round(self.uptime_rate * 30, 1),
                },
                "security_penalty": {"warnings": self.security_warnings, "points": -self.security_penalty},
            },
            "stats": {
                "completed_jobs": self.completed_jobs,
                "failed_jobs": self.failed_jobs,
                "total_frames": self.total_frames,
                "total_processing_seconds": round(self.total_processing_seconds, 1),
                "total_heartbeats": self.total_heartbeats,
                "missed_heartbeats": self.missed_heartbeats,
            },
            "last_updated": self.last_updated,
        }


def _load_reputations() -> dict[str, dict]:
    from .database import get_storage

    return get_storage().get_setting("node_reputations", {})


def _save_reputations(reps: dict[str, dict]) -> None:
    from .database import get_storage

    get_storage().set_setting("node_reputations", reps)


def get_reputation(node_id: str) -> NodeReputation:
    """Get reputation for a node."""
    reps = _load_reputations()
    data = reps.get(node_id)
    if data:
        return NodeReputation(**data)
    return NodeReputation(node_id=node_id)


def get_all_reputations() -> list[NodeReputation]:
    """Get all node reputations."""
    reps = _load_reputations()
    return [NodeReputation(**v) for v in reps.values()]


def record_job_completed(node_id: str, frames: int, duration_seconds: float) -> NodeReputation:
    """Record a successful job completion for a node."""
    reps = _load_reputations()
    data = reps.get(node_id, {"node_id": node_id})
    rep = NodeReputation(**data)
    rep.completed_jobs += 1
    rep.total_frames += frames
    rep.total_processing_seconds += max(0, duration_seconds)
    rep.last_updated = time.time()
    reps[node_id] = rep.__dict__
    _save_reputations(reps)

    # Check auto-pause threshold
    if rep.score < AUTO_PAUSE_THRESHOLD and (rep.completed_jobs + rep.failed_jobs) >= 3:
        logger.warning(f"Node {node_id} reputation {rep.score} below threshold {AUTO_PAUSE_THRESHOLD}")
        _auto_pause_node(node_id)

    return rep


def record_job_failed(node_id: str) -> NodeReputation:
    """Record a failed job for a node."""
    reps = _load_reputations()
    data = reps.get(node_id, {"node_id": node_id})
    rep = NodeReputation(**data)
    rep.failed_jobs += 1
    rep.last_updated = time.time()
    reps[node_id] = rep.__dict__
    _save_reputations(reps)

    if rep.score < AUTO_PAUSE_THRESHOLD and (rep.completed_jobs + rep.failed_jobs) >= 3:
        logger.warning(f"Node {node_id} reputation {rep.score} below threshold {AUTO_PAUSE_THRESHOLD}")
        _auto_pause_node(node_id)

    return rep


def record_heartbeat(node_id: str, on_time: bool = True) -> None:
    """Record a heartbeat for uptime tracking."""
    reps = _load_reputations()
    data = reps.get(node_id, {"node_id": node_id})
    rep = NodeReputation(**data)
    rep.total_heartbeats += 1
    if not on_time:
        rep.missed_heartbeats += 1
    reps[node_id] = rep.__dict__
    _save_reputations(reps)


def record_security_warning(node_id: str, warnings: list[str]) -> None:
    """Record security warnings from node registration. Clears penalty if no warnings."""
    reps = _load_reputations()
    data = reps.get(node_id, {"node_id": node_id})
    rep = NodeReputation(**data)
    rep.security_warnings = len(warnings)
    rep.last_updated = time.time()
    reps[node_id] = rep.__dict__
    _save_reputations(reps)
    logger.info(f"Node {node_id} security penalty: {rep.security_penalty} pts for {warnings}")


def _auto_pause_node(node_id: str) -> None:
    """Auto-pause a node with low reputation."""
    try:
        from .deps import get_node_state

        node = get_node_state().get_node(node_id)
        if node and not node.paused:
            node.paused = True
            logger.info(f"Auto-paused node {node_id} due to low reputation")
    except Exception:
        pass
