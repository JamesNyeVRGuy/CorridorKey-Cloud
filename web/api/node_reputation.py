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


_REP_COLS = (
    "node_id, completed_jobs, failed_jobs, total_frames, total_processing_seconds, "
    "missed_heartbeats, total_heartbeats, security_warnings, last_updated"
)


def _row_to_rep(row: tuple) -> NodeReputation:
    return NodeReputation(
        node_id=row[0],
        completed_jobs=int(row[1] or 0),
        failed_jobs=int(row[2] or 0),
        total_frames=int(row[3] or 0),
        total_processing_seconds=float(row[4] or 0),
        missed_heartbeats=int(row[5] or 0),
        total_heartbeats=int(row[6] or 0),
        security_warnings=int(row[7] or 0),
        last_updated=float(row[8] or 0),
    )


def _load_reputations() -> dict[str, dict]:
    from .database import get_storage

    return get_storage().get_setting("node_reputations", {})


def _save_reputations(reps: dict[str, dict]) -> None:
    from .database import get_storage

    get_storage().set_setting("node_reputations", reps)


def get_reputation(node_id: str) -> NodeReputation:
    """Get reputation for a node."""
    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_REP_COLS} FROM ck.node_reputations WHERE node_id = %s",
                (node_id,),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                return _row_to_rep(row)
            return NodeReputation(node_id=node_id)

    data = _load_reputations().get(node_id)
    return NodeReputation(**data) if data else NodeReputation(node_id=node_id)


def get_all_reputations() -> list[NodeReputation]:
    """Get all node reputations."""
    from .database import get_pg_conn

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(f"SELECT {_REP_COLS} FROM ck.node_reputations")
            rows = cur.fetchall()
            cur.close()
            return [_row_to_rep(r) for r in rows]

    return [NodeReputation(**v) for v in _load_reputations().values()]


def _check_auto_pause(rep: NodeReputation) -> None:
    if rep.score < AUTO_PAUSE_THRESHOLD and (rep.completed_jobs + rep.failed_jobs) >= 3:
        logger.warning(f"Node {rep.node_id} reputation {rep.score} below threshold {AUTO_PAUSE_THRESHOLD}")
        _auto_pause_node(rep.node_id)


def record_job_completed(node_id: str, frames: int, duration_seconds: float) -> NodeReputation:
    """Record a successful job completion for a node.

    Postgres path is an atomic INSERT ... ON CONFLICT DO UPDATE SET
    x = x + ? so parallel completions cannot lose increments.
    """
    from .database import get_pg_conn

    now = time.time()
    duration = max(0.0, float(duration_seconds))

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                f"""INSERT INTO ck.node_reputations (
                        node_id, completed_jobs, total_frames,
                        total_processing_seconds, last_updated)
                    VALUES (%s, 1, %s, %s, %s)
                    ON CONFLICT (node_id) DO UPDATE SET
                        completed_jobs = ck.node_reputations.completed_jobs + 1,
                        total_frames = ck.node_reputations.total_frames + EXCLUDED.total_frames,
                        total_processing_seconds = ck.node_reputations.total_processing_seconds
                            + EXCLUDED.total_processing_seconds,
                        last_updated = EXCLUDED.last_updated
                    RETURNING {_REP_COLS}""",
                (node_id, frames, duration, now),
            )
            row = cur.fetchone()
            cur.close()
            rep = _row_to_rep(row)
            _check_auto_pause(rep)
            return rep

    reps = _load_reputations()
    data = reps.get(node_id, {"node_id": node_id})
    rep = NodeReputation(**data)
    rep.completed_jobs += 1
    rep.total_frames += frames
    rep.total_processing_seconds += duration
    rep.last_updated = now
    reps[node_id] = rep.__dict__
    _save_reputations(reps)
    _check_auto_pause(rep)
    return rep


def record_job_failed(node_id: str) -> NodeReputation:
    """Record a failed job for a node (atomic counter increment on PG)."""
    from .database import get_pg_conn

    now = time.time()

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                f"""INSERT INTO ck.node_reputations (node_id, failed_jobs, last_updated)
                    VALUES (%s, 1, %s)
                    ON CONFLICT (node_id) DO UPDATE SET
                        failed_jobs = ck.node_reputations.failed_jobs + 1,
                        last_updated = EXCLUDED.last_updated
                    RETURNING {_REP_COLS}""",
                (node_id, now),
            )
            row = cur.fetchone()
            cur.close()
            rep = _row_to_rep(row)
            _check_auto_pause(rep)
            return rep

    reps = _load_reputations()
    data = reps.get(node_id, {"node_id": node_id})
    rep = NodeReputation(**data)
    rep.failed_jobs += 1
    rep.last_updated = now
    reps[node_id] = rep.__dict__
    _save_reputations(reps)
    _check_auto_pause(rep)
    return rep


def record_heartbeat(node_id: str, on_time: bool = True) -> None:
    """Record a heartbeat for uptime tracking (atomic on PG)."""
    from .database import get_pg_conn

    miss = 0 if on_time else 1

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO ck.node_reputations (node_id, total_heartbeats, missed_heartbeats)
                   VALUES (%s, 1, %s)
                   ON CONFLICT (node_id) DO UPDATE SET
                       total_heartbeats = ck.node_reputations.total_heartbeats + 1,
                       missed_heartbeats = ck.node_reputations.missed_heartbeats
                           + EXCLUDED.missed_heartbeats""",
                (node_id, miss),
            )
            cur.close()
            return

    reps = _load_reputations()
    data = reps.get(node_id, {"node_id": node_id})
    rep = NodeReputation(**data)
    rep.total_heartbeats += 1
    if not on_time:
        rep.missed_heartbeats += 1
    reps[node_id] = rep.__dict__
    _save_reputations(reps)


def record_security_warning(node_id: str, warnings: list[str]) -> None:
    """Record security warnings from node registration. Clears penalty if no warnings.

    This is an assignment (not an increment), so it's an UPSERT with
    SET = EXCLUDED rather than SET = x + ?.
    """
    from .database import get_pg_conn

    count = len(warnings)
    now = time.time()

    with get_pg_conn() as conn:
        if conn is not None:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO ck.node_reputations (node_id, security_warnings, last_updated)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (node_id) DO UPDATE SET
                       security_warnings = EXCLUDED.security_warnings,
                       last_updated = EXCLUDED.last_updated""",
                (node_id, count, now),
            )
            cur.close()
            penalty = min(15, count * 5)
            logger.info(f"Node {node_id} security penalty: {penalty} pts for {warnings}")
            return

    reps = _load_reputations()
    data = reps.get(node_id, {"node_id": node_id})
    rep = NodeReputation(**data)
    rep.security_warnings = count
    rep.last_updated = now
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
