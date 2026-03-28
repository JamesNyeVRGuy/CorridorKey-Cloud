"""Tests for Redis-backed state backend (CRKY-105 Phase 2).

Uses fakeredis to test without a real Redis server.
"""

from __future__ import annotations

import json
import time

import fakeredis
import pytest

from backend.job_queue import GPUJob, JobStatus, JobType
from web.api.nodes import GPUSlot, NodeInfo, NodeSchedule

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_redis(monkeypatch):
    """Provide a fakeredis client and wire it into redis_client."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr("web.api.redis_client._client", client)
    monkeypatch.setattr("web.api.redis_client._REDIS_URL", "redis://fake:6379/0")
    # Clear Lua script cache so scripts get loaded on the fake server
    import web.api.redis_client as rc

    rc._script_shas.clear()
    return client


@pytest.fixture()
def node_state(fake_redis):
    from web.api.redis_state import RedisNodeState

    return RedisNodeState()


@pytest.fixture()
def job_state(fake_redis):
    from web.api.redis_state import RedisJobState

    return RedisJobState()


def _make_node(node_id: str = "abc123", name: str = "test-node", **kwargs) -> NodeInfo:
    defaults = dict(
        node_id=node_id,
        name=name,
        host="192.168.1.10",
        gpu_name="RTX 4090",
        vram_total_gb=24.0,
        vram_free_gb=20.0,
    )
    defaults.update(kwargs)
    return NodeInfo(**defaults)


def _make_job(clip_name: str = "shot1", job_type: JobType = JobType.INFERENCE, **kwargs) -> GPUJob:
    job = GPUJob(job_type=job_type, clip_name=clip_name)
    for k, v in kwargs.items():
        setattr(job, k, v)
    return job


# ---------------------------------------------------------------------------
# Serialization round-trip tests
# ---------------------------------------------------------------------------


class TestGPUJobSerialization:
    def test_round_trip(self):
        job = _make_job(
            priority=5,
            shard_group="sg-1",
            shard_index=2,
            shard_total=4,
            preferred_node="node-x",
            org_id="org-1",
            submitted_by="user-1",
            params={"model": "v2", "resolution": 2048},
        )
        job.status = JobStatus.RUNNING
        job.claimed_by = "node-y"
        job.started_at = 1234567890.0
        job.current_frame = 50
        job.total_frames = 100

        restored = GPUJob.from_dict(job.to_dict())
        assert restored.id == job.id
        assert restored.job_type == job.job_type
        assert restored.clip_name == job.clip_name
        assert restored.status == job.status
        assert restored.priority == job.priority
        assert restored.shard_group == job.shard_group
        assert restored.shard_index == job.shard_index
        assert restored.shard_total == job.shard_total
        assert restored.preferred_node == job.preferred_node
        assert restored.org_id == job.org_id
        assert restored.submitted_by == job.submitted_by
        assert restored.params == job.params
        assert restored.claimed_by == job.claimed_by
        assert restored.started_at == job.started_at
        assert restored.current_frame == job.current_frame
        assert restored.total_frames == job.total_frames

    def test_round_trip_via_json(self):
        job = _make_job(params={"nested": {"a": 1}})
        restored = GPUJob.from_dict(json.loads(json.dumps(job.to_dict())))
        assert restored.id == job.id
        assert restored.params == {"nested": {"a": 1}}


class TestNodeInfoSerialization:
    def test_round_trip(self):
        node = _make_node(
            gpus=[GPUSlot(index=0, name="RTX 4090", vram_total_gb=24.0, vram_free_gb=20.0)],
            schedule=NodeSchedule(enabled=True, start="09:00", end="17:00"),
            paused=True,
            accepted_types=["inference", "gvm_alpha"],
            org_id="org-1",
            health_history=[{"ts": 1.0, "cpu": 50}],
            recent_logs=["line1", "line2"],
        )
        restored = NodeInfo.from_dict(node.to_storage_dict())
        assert restored.node_id == node.node_id
        assert restored.name == node.name
        assert restored.gpus[0].name == "RTX 4090"
        assert restored.schedule.enabled is True
        assert restored.schedule.start == "09:00"
        assert restored.paused is True
        assert restored.accepted_types == ["inference", "gvm_alpha"]
        assert restored.health_history == [{"ts": 1.0, "cpu": 50}]
        assert restored.recent_logs == ["line1", "line2"]


# ---------------------------------------------------------------------------
# RedisNodeState tests
# ---------------------------------------------------------------------------


class TestRedisNodeState:
    def test_register_and_get(self, node_state):
        info = _make_node()
        node_state.register(info)
        node = node_state.get_node("abc123")
        assert node is not None
        assert node.name == "test-node"
        assert node.status == "online"

    def test_heartbeat_updates_and_keeps_alive(self, node_state):
        node_state.register(_make_node())
        assert node_state.heartbeat("abc123", vram_free_gb=18.0, status="busy")
        node = node_state.get_node("abc123")
        assert node.vram_free_gb == 18.0
        assert node.status == "busy"

    def test_heartbeat_unknown_node_returns_false(self, node_state):
        assert not node_state.heartbeat("nonexistent")

    def test_unregister(self, node_state):
        node_state.register(_make_node())
        node_state.unregister("abc123")
        assert node_state.get_node("abc123") is None

    def test_dismiss(self, node_state):
        node_state.register(_make_node())
        node_state.unregister("abc123", dismiss=True)
        assert node_state.is_dismissed("abc123")

    def test_dismiss_cleared_on_re_register(self, node_state):
        node_state.register(_make_node())
        node_state.unregister("abc123", dismiss=True)
        assert node_state.is_dismissed("abc123")
        node_state.register(_make_node())
        assert not node_state.is_dismissed("abc123")

    def test_set_busy_and_idle(self, node_state):
        node_state.register(_make_node())
        node_state.set_busy("abc123", "job-1")
        node = node_state.get_node("abc123")
        assert node.status == "busy"
        assert node.current_job_id == "job-1"

        node_state.set_idle("abc123")
        node = node_state.get_node("abc123")
        assert node.status == "online"
        assert node.current_job_id is None

    def test_list_nodes(self, node_state):
        node_state.register(_make_node("n1", "Node 1"))
        node_state.register(_make_node("n2", "Node 2"))
        nodes = node_state.list_nodes()
        assert len(nodes) == 2
        names = {n.name for n in nodes}
        assert names == {"Node 1", "Node 2"}

    def test_online_count(self, node_state):
        node_state.register(_make_node("n1", "Node 1"))
        node_state.register(_make_node("n2", "Node 2"))
        assert node_state.online_count == 2

    def test_re_register_preserves_ui_settings(self, node_state):
        info = _make_node()
        node_state.register(info)
        # Simulate UI setting paused via set_busy pattern (direct mutation)
        node = node_state.get_node("abc123")
        node.paused = True
        # Save it back manually for Redis (would normally be done by route handler)
        import web.api.redis_state as rs

        get_redis = __import__("web.api.redis_client", fromlist=["get_redis"]).get_redis
        r = get_redis()
        r.set(rs._node_key("abc123"), rs._save_node(node))

        # Re-register with new hardware info
        info2 = _make_node(gpu_name="RTX 5090")
        node_state.register(info2)
        node = node_state.get_node("abc123")
        assert node.gpu_name == "RTX 5090"  # updated
        assert node.paused is True  # preserved from before


# ---------------------------------------------------------------------------
# RedisJobState tests
# ---------------------------------------------------------------------------


class TestRedisJobState:
    def test_submit_and_find(self, job_state):
        job = _make_job()
        assert job_state.submit(job)
        found = job_state.find_job_by_id(job.id)
        assert found is not None
        assert found.clip_name == "shot1"
        assert found.status == JobStatus.QUEUED

    def test_submit_dedup(self, job_state):
        job1 = _make_job("shot1")
        job2 = _make_job("shot1")
        assert job_state.submit(job1)
        assert not job_state.submit(job2)

    def test_submit_sharded_bypasses_dedup(self, job_state):
        job1 = _make_job("shot1", shard_group="sg-1", shard_index=0, shard_total=2)
        job2 = _make_job("shot1", shard_group="sg-1", shard_index=1, shard_total=2)
        assert job_state.submit(job1)
        assert job_state.submit(job2)

    def test_claim_job(self, job_state):
        job = _make_job()
        job_state.submit(job)
        claimed = job_state.claim_job("node-1")
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == JobStatus.RUNNING
        assert claimed.claimed_by == "node-1"
        # Queue should be empty now
        assert not job_state.has_pending

    def test_claim_respects_preferred_node(self, job_state):
        job = _make_job(preferred_node="node-1")
        job_state.submit(job)
        # node-2 can't claim it
        assert job_state.claim_job("node-2") is None
        # node-1 can
        claimed = job_state.claim_job("node-1")
        assert claimed is not None

    def test_claim_skips_local_only_for_remote(self, job_state):
        job = _make_job(job_type=JobType.VIDEO_EXTRACT)
        job_state.submit(job)
        # Remote node can't claim extract jobs
        assert job_state.claim_job("remote-node") is None
        # Local can
        claimed = job_state.claim_job("local")
        assert claimed is not None

    def test_claim_respects_accepted_types(self, job_state):
        job = _make_job(job_type=JobType.GVM_ALPHA)
        job_state.submit(job)
        # Node that only accepts inference
        assert job_state.claim_job("node-1", accepted_types=["inference"]) is None
        # Node that accepts gvm_alpha
        claimed = job_state.claim_job("node-1", accepted_types=["gvm_alpha"])
        assert claimed is not None

    def test_claim_respects_org_isolation(self, job_state):
        job = _make_job(org_id="org-A")
        job_state.submit(job)
        # Different org can't claim
        assert job_state.claim_job("node-1", org_id="org-B") is None
        # Same org can
        claimed = job_state.claim_job("node-1", org_id="org-A")
        assert claimed is not None

    def test_claim_priority_order(self, job_state):
        low = _make_job("low", priority=0)
        high = _make_job("high", priority=10)
        job_state.submit(low)
        job_state.submit(high)
        # High priority should be claimed first
        claimed = job_state.claim_job("local")
        assert claimed.clip_name == "high"

    def test_complete_job(self, job_state):
        job = _make_job()
        job_state.submit(job)
        claimed = job_state.claim_job("local")
        job_state.complete_job(claimed)
        found = job_state.find_job_by_id(claimed.id)
        assert found.status == JobStatus.COMPLETED
        assert len(job_state.running_jobs) == 0
        assert len(job_state.history_snapshot) == 1

    def test_fail_job(self, job_state):
        job = _make_job()
        job_state.submit(job)
        claimed = job_state.claim_job("local")
        job_state.fail_job(claimed, "out of VRAM")
        found = job_state.find_job_by_id(claimed.id)
        assert found.status == JobStatus.FAILED
        assert found.error_message == "out of VRAM"

    def test_requeue_job(self, job_state):
        job = _make_job()
        job_state.submit(job)
        claimed = job_state.claim_job("local")
        job_state.requeue_job(claimed)
        assert job_state.has_pending
        assert len(job_state.running_jobs) == 0

    def test_cancel_queued_job(self, job_state):
        job = _make_job()
        job_state.submit(job)
        found = job_state.find_job_by_id(job.id)
        job_state.cancel_job(found)
        assert not job_state.has_pending
        assert len(job_state.history_snapshot) == 1
        assert job_state.find_job_by_id(job.id).status == JobStatus.CANCELLED

    def test_cancel_running_job_sets_flag(self, job_state, fake_redis):
        job = _make_job()
        job_state.submit(job)
        claimed = job_state.claim_job("local")
        job_state.cancel_job(claimed)
        # Cancel flag should be set in Redis
        assert fake_redis.get(f"ck:job:{claimed.id}:cancel") == "1"

    def test_cancel_all(self, job_state):
        job_state.submit(_make_job("a"))
        job_state.submit(_make_job("b"))
        job_state.claim_job("local")
        job_state.cancel_all()
        # Queued jobs moved to history
        assert not job_state.has_pending
        # Running job has cancel flag
        assert len(job_state.history_snapshot) >= 1

    def test_properties(self, job_state):
        assert not job_state.has_pending
        assert job_state.pending_count == 0
        assert job_state.current_job is None
        assert job_state.running_jobs == []

        job_state.submit(_make_job("a"))
        job_state.submit(_make_job("b"))
        assert job_state.has_pending
        assert job_state.pending_count == 2

        job_state.claim_job("local")
        assert job_state.pending_count == 1
        assert job_state.current_job is not None
        assert len(job_state.running_jobs) == 1

    def test_history_trimming(self, job_state, monkeypatch):
        monkeypatch.setattr("web.api.redis_state._MAX_HISTORY", 5)
        for i in range(8):
            job = _make_job(f"clip-{i}")
            job_state.submit(job)
            claimed = job_state.claim_job("local")
            job_state.complete_job(claimed)
        assert len(job_state.history_snapshot) == 5

    def test_shard_group_progress(self, job_state):
        for i in range(3):
            job = _make_job(f"shot-{i}", shard_group="sg-1", shard_index=i, shard_total=3)
            job_state.submit(job)
        # Complete first shard
        claimed = job_state.claim_job("local")
        claimed.total_frames = 100
        claimed.current_frame = 100
        job_state.complete_job(claimed)

        progress = job_state.shard_group_progress("sg-1")
        assert progress["total_shards"] == 3
        assert progress["completed"] == 1

    def test_shard_group_all_done(self, job_state):
        for i in range(2):
            job = _make_job(f"shot-{i}", shard_group="sg-1", shard_index=i, shard_total=2)
            job_state.submit(job)
        assert not job_state.shard_group_all_done("sg-1")
        # Complete both
        for _ in range(2):
            claimed = job_state.claim_job("local")
            if claimed:
                job_state.complete_job(claimed)
        assert job_state.shard_group_all_done("sg-1")

    def test_restore_history(self, job_state):
        jobs = [_make_job(f"hist-{i}") for i in range(3)]
        for j in jobs:
            j.status = JobStatus.COMPLETED
            j.completed_at = time.time()
        job_state.restore_history(jobs)
        assert len(job_state.history_snapshot) == 3

    def test_restore_history_noop_if_exists(self, job_state):
        # First restore
        jobs = [_make_job("hist-0")]
        jobs[0].status = JobStatus.COMPLETED
        jobs[0].completed_at = time.time()
        job_state.restore_history(jobs)
        # Second restore should be no-op
        jobs2 = [_make_job("hist-1")]
        jobs2[0].status = JobStatus.COMPLETED
        jobs2[0].completed_at = time.time()
        job_state.restore_history(jobs2)
        assert len(job_state.history_snapshot) == 1  # still 1, not 2

    def test_clear_history(self, job_state):
        job = _make_job()
        job_state.submit(job)
        claimed = job_state.claim_job("local")
        job_state.complete_job(claimed)
        assert len(job_state.history_snapshot) == 1
        job_state.clear_history()
        assert len(job_state.history_snapshot) == 0

    def test_remove_job(self, job_state):
        job = _make_job()
        job_state.submit(job)
        claimed = job_state.claim_job("local")
        job_state.complete_job(claimed)
        job_state.remove_job(claimed.id)
        assert len(job_state.history_snapshot) == 0

    def test_preview_reprocess_replaces(self, job_state):
        job1 = _make_job("clip1", job_type=JobType.PREVIEW_REPROCESS)
        job2 = _make_job("clip1", job_type=JobType.PREVIEW_REPROCESS)
        assert job_state.submit(job1)
        assert job_state.submit(job2)
        # Only the latest should be in queue
        assert job_state.pending_count == 1
        queued = job_state.queue_snapshot
        assert queued[0].id == job2.id

    def test_callbacks_fire(self, job_state):
        completed_clips = []
        job_state.on_completion = lambda clip: completed_clips.append(clip)

        job = _make_job("shot1")
        job_state.submit(job)
        claimed = job_state.claim_job("local")
        job_state.complete_job(claimed)
        assert completed_clips == ["shot1"]
