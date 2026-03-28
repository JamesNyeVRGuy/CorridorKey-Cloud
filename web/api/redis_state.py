"""Redis-backed state backend for multi-instance deployment (CRKY-105 Phase 2).

Implements NodeState and JobState protocols using Redis as the shared store.
Selected when CK_REDIS_URL is set; otherwise the server uses InMemoryState.

Key schema:
    ck:node:{id}          — JSON of NodeInfo (to_storage_dict)
    ck:node:{id}:alive    — heartbeat sentinel with 60s TTL
    ck:nodes              — SET of all registered node IDs
    ck:nodes:dismissed    — SET of dismissed node IDs
    ck:job:{id}           — JSON of GPUJob (to_dict)
    ck:queue              — SORTED SET of pending job IDs (score = -(priority * 1e12 + time_ns))
    ck:jobs:running       — SET of running job IDs
    ck:jobs:history       — SORTED SET of history job IDs (score = completion_time)
    ck:job:{id}:cancel    — cancel flag for running jobs
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from backend.job_queue import GPUJob, JobStatus, JobType

from .nodes import NodeInfo
from .redis_client import get_redis, run_script

logger = logging.getLogger(__name__)

# Callback type aliases
ProgressCallback = Callable[[str, int, int], None]
WarningCallback = Callable[[str], None]
CompletionCallback = Callable[[str], None]
ErrorCallback = Callable[[str, str], None]

_MAX_HISTORY = 1000
_HEARTBEAT_TTL = 60  # seconds
_PROGRESS_DEBOUNCE_FRAMES = 5  # only write to Redis every N frames


# ---------------------------------------------------------------------------
# Lua scripts
# ---------------------------------------------------------------------------

_LUA_CLAIM_JOB = """\
local queue_key = KEYS[1]
local running_key = KEYS[2]
local claimer_id = ARGV[1]
local accepted_types_json = ARGV[2]
local org_id = ARGV[3]
local local_only_json = ARGV[4]
local now = tonumber(ARGV[5])

local accepted_types = {}
if accepted_types_json ~= "" then
    accepted_types = cjson.decode(accepted_types_json)
end
local local_only = cjson.decode(local_only_json)

local function contains(arr, val)
    for _, v in ipairs(arr) do
        if v == val then return true end
    end
    return false
end

local function is_null(v)
    return v == nil or v == cjson.null or v == ""
end

local job_ids = redis.call('ZRANGE', queue_key, 0, -1)
for _, job_id in ipairs(job_ids) do
    local job_json = redis.call('GET', 'ck:job:' .. job_id)
    if job_json then
        local job = cjson.decode(job_json)
        local skip = false
        if not is_null(job.preferred_node) and job.preferred_node ~= claimer_id then
            skip = true
        end
        if not skip and claimer_id ~= "local" and contains(local_only, job.job_type) then
            skip = true
        end
        if not skip and #accepted_types > 0 and not contains(accepted_types, job.job_type) then
            skip = true
        end
        if not skip and org_id ~= "" and not is_null(job.org_id) and job.org_id ~= org_id then
            skip = true
        end
        if not skip then
            redis.call('ZREM', queue_key, job_id)
            redis.call('SADD', running_key, job_id)
            job.status = "running"
            job.claimed_by = claimer_id
            job.started_at = now
            redis.call('SET', 'ck:job:' .. job_id, cjson.encode(job))
            return job_id
        end
    end
end
return nil
"""

_LUA_SUBMIT_JOB = """\
local queue_key = KEYS[1]
local running_key = KEYS[2]
local job_id = ARGV[1]
local job_json = ARGV[2]
local score = tonumber(ARGV[3])
local job_type = ARGV[4]
local clip_name = ARGV[5]
local shard_group = ARGV[6]
local is_preview = ARGV[7]

if is_preview == "1" then
    local queued_ids = redis.call('ZRANGE', queue_key, 0, -1)
    for _, qid in ipairs(queued_ids) do
        local qjson = redis.call('GET', 'ck:job:' .. qid)
        if qjson then
            local qjob = cjson.decode(qjson)
            if qjob.job_type == "preview_reprocess" then
                redis.call('ZREM', queue_key, qid)
                qjob.status = "cancelled"
                redis.call('SET', 'ck:job:' .. qid, cjson.encode(qjob))
            end
        end
    end
else
    if shard_group == "" then
        local queued_ids = redis.call('ZRANGE', queue_key, 0, -1)
        for _, qid in ipairs(queued_ids) do
            local qjson = redis.call('GET', 'ck:job:' .. qid)
            if qjson then
                local qjob = cjson.decode(qjson)
                if qjob.clip_name == clip_name and qjob.job_type == job_type then
                    return 0
                end
            end
        end
        local running_ids = redis.call('SMEMBERS', running_key)
        for _, rid in ipairs(running_ids) do
            local rjson = redis.call('GET', 'ck:job:' .. rid)
            if rjson then
                local rjob = cjson.decode(rjson)
                if rjob.clip_name == clip_name and rjob.job_type == job_type
                   and rjob.status == "running" then
                    return 0
                end
            end
        end
    end
end

redis.call('SET', 'ck:job:' .. job_id, job_json)
redis.call('ZADD', queue_key, score, job_id)
return 1
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _node_key(node_id: str) -> str:
    return f"ck:node:{node_id}"


def _alive_key(node_id: str) -> str:
    return f"ck:node:{node_id}:alive"


def _job_key(job_id: str) -> str:
    return f"ck:job:{job_id}"


def _cancel_key(job_id: str) -> str:
    return f"ck:job:{job_id}:cancel"


def _load_node(data: str | None) -> NodeInfo | None:
    if data is None:
        return None
    return NodeInfo.from_dict(json.loads(data))


def _load_job(data: str | None) -> GPUJob | None:
    if data is None:
        return None
    return GPUJob.from_dict(json.loads(data))


def _save_node(node: NodeInfo) -> str:
    return json.dumps(node.to_storage_dict())


def _save_job(job: GPUJob) -> str:
    return json.dumps(job.to_dict())


# ---------------------------------------------------------------------------
# RedisNodeState
# ---------------------------------------------------------------------------


class RedisNodeState:
    """Node registry backed by Redis."""

    def register(self, info: NodeInfo) -> None:
        r = get_redis()
        pipe = r.pipeline()
        pipe.srem("ck:nodes:dismissed", info.node_id)

        # On re-register: merge new fields into existing node, preserve UI settings
        existing_json = r.get(_node_key(info.node_id))
        if existing_json:
            existing = NodeInfo.from_dict(json.loads(existing_json))
            info.paused = existing.paused
            info.schedule = existing.schedule
            info.accepted_types = existing.accepted_types
            info.health_history = existing.health_history
            info.recent_logs = existing.recent_logs
            if not info.org_id:
                info.org_id = existing.org_id

        info.status = "online"
        info.last_heartbeat = time.time()
        pipe.set(_node_key(info.node_id), _save_node(info))
        pipe.sadd("ck:nodes", info.node_id)
        pipe.set(_alive_key(info.node_id), "1", ex=_HEARTBEAT_TTL)
        pipe.execute()
        logger.info(f"Node registered: {info.name} ({info.node_id})")

    def heartbeat(self, node_id: str, vram_free_gb: float = 0.0, status: str = "online") -> bool:
        r = get_redis()
        data = r.get(_node_key(node_id))
        if data is None:
            return False
        node = NodeInfo.from_dict(json.loads(data))
        node.last_heartbeat = time.time()
        node.vram_free_gb = vram_free_gb
        node.status = status
        pipe = r.pipeline()
        pipe.set(_node_key(node_id), _save_node(node))
        pipe.set(_alive_key(node_id), "1", ex=_HEARTBEAT_TTL)
        pipe.execute()
        return True

    def unregister(self, node_id: str, dismiss: bool = False) -> None:
        r = get_redis()
        pipe = r.pipeline()
        pipe.delete(_node_key(node_id), _alive_key(node_id))
        pipe.srem("ck:nodes", node_id)
        if dismiss:
            pipe.sadd("ck:nodes:dismissed", node_id)
        pipe.execute()
        logger.info(f"Node unregistered: {node_id}")

    def set_busy(self, node_id: str, job_id: str) -> None:
        r = get_redis()
        data = r.get(_node_key(node_id))
        if data is None:
            return
        node = NodeInfo.from_dict(json.loads(data))
        node.status = "busy"
        node.current_job_id = job_id
        r.set(_node_key(node_id), _save_node(node))

    def set_idle(self, node_id: str) -> None:
        r = get_redis()
        data = r.get(_node_key(node_id))
        if data is None:
            return
        node = NodeInfo.from_dict(json.loads(data))
        node.status = "online"
        node.current_job_id = None
        r.set(_node_key(node_id), _save_node(node))

    def get_node(self, node_id: str) -> NodeInfo | None:
        r = get_redis()
        data = r.get(_node_key(node_id))
        node = _load_node(data)
        if node is not None:
            # Patch liveness from TTL key (authoritative for Redis backend)
            alive = r.exists(_alive_key(node_id))
            if not alive:
                node.last_heartbeat = 0  # force is_alive → False
        return node

    def list_nodes(self) -> list[NodeInfo]:
        r = get_redis()
        node_ids = r.smembers("ck:nodes")
        if not node_ids:
            return []
        # Batch fetch all node data
        keys = [_node_key(nid) for nid in node_ids]
        values = r.mget(keys)
        alive_pipe = r.pipeline()
        for nid in node_ids:
            alive_pipe.exists(_alive_key(nid))
        alive_flags = alive_pipe.execute()

        nodes = []
        for data, alive in zip(values, alive_flags, strict=True):
            node = _load_node(data)
            if node is not None:
                if not alive:
                    node.last_heartbeat = 0
                nodes.append(node)
        return nodes

    def get_available_node(self, min_vram_gb: float = 0.0) -> NodeInfo | None:
        for node in self.list_nodes():
            if node.is_alive and node.status == "online":
                if min_vram_gb <= 0 or node.vram_free_gb >= min_vram_gb:
                    return node
        return None

    def is_dismissed(self, node_id: str) -> bool:
        return bool(get_redis().sismember("ck:nodes:dismissed", node_id))

    @property
    def online_count(self) -> int:
        r = get_redis()
        node_ids = r.smembers("ck:nodes")
        if not node_ids:
            return 0
        pipe = r.pipeline()
        for nid in node_ids:
            pipe.exists(_alive_key(nid))
        return sum(pipe.execute())


# ---------------------------------------------------------------------------
# RedisJobState
# ---------------------------------------------------------------------------

_LOCAL_ONLY_TYPES = [JobType.VIDEO_EXTRACT.value, JobType.VIDEO_STITCH.value]


class RedisJobState:
    """Job queue backed by Redis."""

    def __init__(self) -> None:
        # Callbacks are instance-local (fired on the instance handling the event)
        self.on_progress: ProgressCallback | None = None
        self.on_warning: WarningCallback | None = None
        self.on_completion: CompletionCallback | None = None
        self.on_error: ErrorCallback | None = None
        self._progress_counters: dict[str, int] = {}  # job_id -> frame count for debounce

    # --- Core operations ---

    def submit(self, job: GPUJob) -> bool:
        job.status = JobStatus.QUEUED
        score = -(job.priority * 1_000_000_000_000 + time.time_ns())
        result = run_script(
            "submit_job",
            _LUA_SUBMIT_JOB,
            keys=["ck:queue", "ck:jobs:running"],
            args=[
                job.id,
                _save_job(job),
                str(score),
                job.job_type.value,
                job.clip_name,
                job.shard_group or "",
                "1" if job.job_type == JobType.PREVIEW_REPROCESS else "0",
            ],
        )
        submitted = int(result) == 1
        if submitted:
            logger.info(f"Job queued [{job.id}]: {job.job_type.value} for '{job.clip_name}'")
        else:
            logger.warning(f"Duplicate job rejected: {job.job_type.value} for '{job.clip_name}'")
        return submitted

    def next_job(self) -> GPUJob | None:
        r = get_redis()
        result = r.zrange("ck:queue", 0, 0)
        if not result:
            return None
        return _load_job(r.get(_job_key(result[0])))

    def claim_job(
        self,
        claimer_id: str = "local",
        accepted_types: list[str] | None = None,
        org_id: str | None = None,
    ) -> GPUJob | None:
        job_id = run_script(
            "claim_job",
            _LUA_CLAIM_JOB,
            keys=["ck:queue", "ck:jobs:running"],
            args=[
                claimer_id,
                json.dumps(accepted_types) if accepted_types else "",
                org_id or "",
                json.dumps(_LOCAL_ONLY_TYPES),
                str(time.time()),
            ],
        )
        if job_id is None:
            return None
        r = get_redis()
        job = _load_job(r.get(_job_key(job_id)))
        if job:
            logger.info(f"Job claimed [{job.id}] by {claimer_id}: {job.job_type.value} for '{job.clip_name}'")
        return job

    def start_job(self, job: GPUJob) -> None:
        r = get_redis()
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        pipe = r.pipeline()
        pipe.zrem("ck:queue", job.id)
        pipe.sadd("ck:jobs:running", job.id)
        pipe.set(_job_key(job.id), _save_job(job))
        pipe.execute()
        logger.info(f"Job started [{job.id}]: {job.job_type.value} for '{job.clip_name}'")

    def complete_job(self, job: GPUJob) -> None:
        r = get_redis()
        job.status = JobStatus.COMPLETED
        job.completed_at = time.time()
        pipe = r.pipeline()
        pipe.srem("ck:jobs:running", job.id)
        pipe.zadd("ck:jobs:history", {job.id: job.completed_at})
        pipe.set(_job_key(job.id), _save_job(job))
        pipe.delete(_cancel_key(job.id))
        pipe.execute()
        self._trim_history()
        logger.info(f"Job completed [{job.id}]: {job.job_type.value} for '{job.clip_name}'")
        self._progress_counters.pop(job.id, None)
        if self.on_completion:
            self.on_completion(job.clip_name)

    def fail_job(self, job: GPUJob, error: str) -> None:
        r = get_redis()
        job.status = JobStatus.FAILED
        job.error_message = error
        pipe = r.pipeline()
        pipe.srem("ck:jobs:running", job.id)
        pipe.zadd("ck:jobs:history", {job.id: time.time()})
        pipe.set(_job_key(job.id), _save_job(job))
        pipe.delete(_cancel_key(job.id))
        pipe.execute()
        self._trim_history()
        logger.error(f"Job failed [{job.id}]: {job.job_type.value} for '{job.clip_name}': {error}")
        self._progress_counters.pop(job.id, None)
        if self.on_error:
            self.on_error(job.clip_name, error)

    def move_job(self, job_id: str, position: int) -> bool:
        r = get_redis()
        # Read current queue order
        ordered = r.zrange("ck:queue", 0, -1, withscores=True)
        found = None
        for jid, score in ordered:
            if jid == job_id:
                found = (jid, score)
                break
        if found is None:
            return False
        # Remove and reinsert at target position by adjusting score
        pos = max(0, min(position, len(ordered) - 1))
        if pos == 0:
            # Move to front: score lower than current first
            new_score = ordered[0][1] - 1 if ordered else 0
        elif pos >= len(ordered) - 1:
            new_score = ordered[-1][1] + 1
        else:
            # Average of neighbors
            new_score = (ordered[pos][1] + ordered[pos - 1][1]) / 2
        pipe = r.pipeline()
        pipe.zrem("ck:queue", job_id)
        pipe.zadd("ck:queue", {job_id: new_score})
        pipe.execute()
        logger.info(f"Job [{job_id}] moved to position {position}")
        return True

    def requeue_job(self, job: GPUJob) -> None:
        r = get_redis()
        job.status = JobStatus.QUEUED
        job.claimed_by = None
        job.current_frame = 0
        job.total_frames = 0
        # Score puts it at front (lowest possible)
        score = -(job.priority * 1_000_000_000_000 + time.time_ns() + 1_000_000_000_000)
        pipe = r.pipeline()
        pipe.srem("ck:jobs:running", job.id)
        pipe.zadd("ck:queue", {job.id: score})
        pipe.set(_job_key(job.id), _save_job(job))
        pipe.delete(_cancel_key(job.id))
        pipe.execute()
        self._progress_counters.pop(job.id, None)
        logger.info(f"Job requeued [{job.id}]: {job.job_type.value} for '{job.clip_name}'")

    def mark_cancelled(self, job: GPUJob) -> None:
        r = get_redis()
        job.status = JobStatus.CANCELLED
        pipe = r.pipeline()
        pipe.srem("ck:jobs:running", job.id)
        pipe.zadd("ck:jobs:history", {job.id: time.time()})
        pipe.set(_job_key(job.id), _save_job(job))
        pipe.delete(_cancel_key(job.id))
        pipe.execute()
        self._trim_history()
        self._progress_counters.pop(job.id, None)
        logger.info(f"Job cancelled [{job.id}]: {job.job_type.value} for '{job.clip_name}'")

    def cancel_job(self, job: GPUJob) -> None:
        r = get_redis()
        if job.status == JobStatus.QUEUED:
            pipe = r.pipeline()
            pipe.zrem("ck:queue", job.id)
            job.status = JobStatus.CANCELLED
            pipe.zadd("ck:jobs:history", {job.id: time.time()})
            pipe.set(_job_key(job.id), _save_job(job))
            pipe.execute()
            self._trim_history()
            logger.info(f"Job removed from queue [{job.id}]")
        elif job.status == JobStatus.RUNNING:
            # Set cancel flag — worker or node will pick it up
            r.set(_cancel_key(job.id), "1")
            job.request_cancel()
            logger.info(f"Job cancel requested [{job.id}]")

    def cancel_current(self) -> None:
        r = get_redis()
        running_ids = r.smembers("ck:jobs:running")
        pipe = r.pipeline()
        for jid in running_ids:
            pipe.set(_cancel_key(jid), "1")
        pipe.execute()

    def cancel_all(self) -> None:
        r = get_redis()
        now = time.time()
        # Cancel running
        running_ids = r.smembers("ck:jobs:running")
        pipe = r.pipeline()
        for jid in running_ids:
            pipe.set(_cancel_key(jid), "1")
        # Move queued to history as cancelled
        queued_ids = r.zrange("ck:queue", 0, -1)
        for qid in queued_ids:
            data = r.get(_job_key(qid))
            if data:
                job = GPUJob.from_dict(json.loads(data))
                job.status = JobStatus.CANCELLED
                pipe.set(_job_key(qid), _save_job(job))
                pipe.zadd("ck:jobs:history", {qid: now})
        pipe.delete("ck:queue")
        pipe.execute()
        self._trim_history()
        logger.info("All jobs cancelled")

    def report_progress(self, clip_name: str, current: int, total: int) -> None:
        r = get_redis()
        running_ids = r.smembers("ck:jobs:running")
        for jid in running_ids:
            data = r.get(_job_key(jid))
            if data:
                job_data = json.loads(data)
                if job_data.get("clip_name") == clip_name and job_data.get("status") == "running":
                    # Debounce: only write to Redis every N frames
                    count = self._progress_counters.get(jid, 0) + 1
                    self._progress_counters[jid] = count
                    if count % _PROGRESS_DEBOUNCE_FRAMES == 0 or current >= total:
                        job_data["current_frame"] = current
                        job_data["total_frames"] = total
                        r.set(_job_key(jid), json.dumps(job_data))
                    break
        if self.on_progress:
            self.on_progress(clip_name, current, total)

    def report_warning(self, message: str) -> None:
        logger.warning(message)
        if self.on_warning:
            self.on_warning(message)

    def find_job_by_id(self, job_id: str) -> GPUJob | None:
        return _load_job(get_redis().get(_job_key(job_id)))

    # --- Shard operations ---

    def _all_shard_jobs(self, shard_group: str) -> list[GPUJob]:
        """Get all jobs in a shard group (scans running + queue + history)."""
        r = get_redis()
        all_ids: set[str] = set()
        all_ids.update(r.smembers("ck:jobs:running"))
        all_ids.update(r.zrange("ck:queue", 0, -1))
        all_ids.update(r.zrange("ck:jobs:history", 0, -1))
        if not all_ids:
            return []
        values = r.mget([_job_key(jid) for jid in all_ids])
        jobs = []
        for data in values:
            job = _load_job(data)
            if job and job.shard_group == shard_group:
                jobs.append(job)
        return jobs

    def shard_group_progress(self, shard_group: str) -> dict[str, Any]:
        shards = self._all_shard_jobs(shard_group)
        if not shards:
            return {
                "total_shards": 0,
                "completed": 0,
                "running": 0,
                "failed": 0,
                "current_frame": 0,
                "total_frames": 0,
            }
        return {
            "shard_group": shard_group,
            "total_shards": len(shards),
            "completed": sum(1 for s in shards if s.status == JobStatus.COMPLETED),
            "running": sum(1 for s in shards if s.status == JobStatus.RUNNING),
            "failed": sum(1 for s in shards if s.status == JobStatus.FAILED),
            "cancelled": sum(1 for s in shards if s.status == JobStatus.CANCELLED),
            "current_frame": sum(s.current_frame for s in shards),
            "total_frames": sum(s.total_frames for s in shards),
        }

    def shard_group_all_done(self, shard_group: str) -> bool:
        if not shard_group:
            return True
        shards = self._all_shard_jobs(shard_group)
        if not shards:
            return True
        terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        return all(s.status in terminal for s in shards)

    def cancel_shard_group(self, shard_group: str) -> int:
        r = get_redis()
        cancelled = 0
        # Cancel queued shards
        queued_ids = r.zrange("ck:queue", 0, -1)
        pipe = r.pipeline()
        for qid in queued_ids:
            data = r.get(_job_key(qid))
            if data:
                job = GPUJob.from_dict(json.loads(data))
                if job.shard_group == shard_group:
                    pipe.zrem("ck:queue", qid)
                    job.status = JobStatus.CANCELLED
                    pipe.zadd("ck:jobs:history", {qid: time.time()})
                    pipe.set(_job_key(qid), _save_job(job))
                    cancelled += 1
        # Set cancel flag on running shards
        running_ids = r.smembers("ck:jobs:running")
        for rid in running_ids:
            data = r.get(_job_key(rid))
            if data:
                job = GPUJob.from_dict(json.loads(data))
                if job.shard_group == shard_group and job.status == JobStatus.RUNNING:
                    pipe.set(_cancel_key(rid), "1")
                    cancelled += 1
        pipe.execute()
        if cancelled:
            logger.info(f"Cancelled {cancelled} shards in group {shard_group}")
        return cancelled

    def retry_failed_shards(self, shard_group: str) -> list[GPUJob]:
        shards = self._all_shard_jobs(shard_group)
        failed = [s for s in shards if s.status == JobStatus.FAILED]
        submitted = []
        for old in failed:
            job = GPUJob(
                job_type=old.job_type,
                clip_name=old.clip_name,
                params=dict(old.params),
                shard_group=old.shard_group,
                shard_index=old.shard_index,
                shard_total=old.shard_total,
                org_id=old.org_id,
                submitted_by=old.submitted_by,
            )
            if self.submit(job):
                submitted.append(job)
        return submitted

    # --- History ---

    def restore_history(self, jobs: list[GPUJob]) -> None:
        """Restore history from persistent storage (no-op if history already exists in Redis)."""
        r = get_redis()
        if r.zcard("ck:jobs:history") > 0:
            return  # Redis already has history, don't overwrite
        if not jobs:
            return
        pipe = r.pipeline()
        for job in jobs:
            ts = job.completed_at if job.completed_at else time.time()
            pipe.set(_job_key(job.id), _save_job(job))
            pipe.zadd("ck:jobs:history", {job.id: ts})
        pipe.execute()
        self._trim_history()

    def clear_history(self) -> None:
        r = get_redis()
        history_ids = r.zrange("ck:jobs:history", 0, -1)
        if history_ids:
            pipe = r.pipeline()
            for jid in history_ids:
                pipe.delete(_job_key(jid))
            pipe.delete("ck:jobs:history")
            pipe.execute()

    def remove_job(self, job_id: str) -> None:
        r = get_redis()
        pipe = r.pipeline()
        pipe.zrem("ck:jobs:history", job_id)
        pipe.delete(_job_key(job_id))
        pipe.execute()

    # --- Read-only properties ---

    @property
    def has_pending(self) -> bool:
        return get_redis().zcard("ck:queue") > 0

    @property
    def current_job(self) -> GPUJob | None:
        r = get_redis()
        running_ids = r.smembers("ck:jobs:running")
        if not running_ids:
            return None
        first_id = next(iter(running_ids))
        return _load_job(r.get(_job_key(first_id)))

    @property
    def running_jobs(self) -> list[GPUJob]:
        r = get_redis()
        running_ids = r.smembers("ck:jobs:running")
        if not running_ids:
            return []
        values = r.mget([_job_key(jid) for jid in running_ids])
        return [j for j in (_load_job(v) for v in values) if j is not None]

    @property
    def pending_count(self) -> int:
        return get_redis().zcard("ck:queue")

    @property
    def queue_snapshot(self) -> list[GPUJob]:
        r = get_redis()
        job_ids = r.zrange("ck:queue", 0, -1)
        if not job_ids:
            return []
        values = r.mget([_job_key(jid) for jid in job_ids])
        return [j for j in (_load_job(v) for v in values) if j is not None]

    @property
    def history_snapshot(self) -> list[GPUJob]:
        r = get_redis()
        job_ids = r.zrange("ck:jobs:history", 0, -1)
        if not job_ids:
            return []
        values = r.mget([_job_key(jid) for jid in job_ids])
        return [j for j in (_load_job(v) for v in values) if j is not None]

    @property
    def all_jobs_snapshot(self) -> list[GPUJob]:
        result = self.running_jobs
        result.extend(self.queue_snapshot)
        result.extend(self.history_snapshot)
        return result

    # --- Internal ---

    def _trim_history(self) -> None:
        """Keep history at most _MAX_HISTORY entries, evict oldest."""
        r = get_redis()
        count = r.zcard("ck:jobs:history")
        if count <= _MAX_HISTORY:
            return
        # Get IDs to evict (oldest = lowest score)
        evict_count = count - _MAX_HISTORY
        evicted = r.zrange("ck:jobs:history", 0, evict_count - 1)
        pipe = r.pipeline()
        pipe.zremrangebyrank("ck:jobs:history", 0, evict_count - 1)
        for jid in evicted:
            pipe.delete(_job_key(jid))
        pipe.execute()
