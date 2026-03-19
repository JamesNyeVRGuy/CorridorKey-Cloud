"""Tests for node job isolation policy (CRKY-19)."""

from backend.job_queue import GPUJob, GPUJobQueue, JobType


class TestJobIsolation:
    def test_claim_without_org_filter(self):
        """No org filter = claim any job."""
        q = GPUJobQueue()
        job = GPUJob(job_type=JobType.INFERENCE, clip_name="clip1", org_id="org-a")
        q.submit(job)
        claimed = q.claim_job("node-1", org_id=None)
        assert claimed is not None
        assert claimed.id == job.id

    def test_claim_matching_org(self):
        """Node in org-a can claim org-a jobs."""
        q = GPUJobQueue()
        job = GPUJob(job_type=JobType.INFERENCE, clip_name="clip1", org_id="org-a")
        q.submit(job)
        claimed = q.claim_job("node-1", org_id="org-a")
        assert claimed is not None

    def test_claim_rejects_different_org(self):
        """Node in org-a cannot claim org-b jobs."""
        q = GPUJobQueue()
        job = GPUJob(job_type=JobType.INFERENCE, clip_name="clip1", org_id="org-b")
        q.submit(job)
        claimed = q.claim_job("node-1", org_id="org-a")
        assert claimed is None

    def test_claim_skips_wrong_org_finds_right_one(self):
        """Node skips jobs from other orgs and claims matching one."""
        q = GPUJobQueue()
        job_b = GPUJob(job_type=JobType.INFERENCE, clip_name="clip-b", org_id="org-b")
        job_a = GPUJob(job_type=JobType.INFERENCE, clip_name="clip-a", org_id="org-a")
        q.submit(job_b)
        q.submit(job_a)
        claimed = q.claim_job("node-1", org_id="org-a")
        assert claimed is not None
        assert claimed.clip_name == "clip-a"
        # org-b job still in queue
        assert len(q.queue_snapshot) == 1

    def test_claim_no_org_on_job(self):
        """Jobs with no org_id (legacy) can be claimed by any node."""
        q = GPUJobQueue()
        job = GPUJob(job_type=JobType.INFERENCE, clip_name="clip1", org_id=None)
        q.submit(job)
        claimed = q.claim_job("node-1", org_id="org-a")
        assert claimed is not None

    def test_local_worker_claims_any(self):
        """Local worker (claimer_id='local') with no org filter claims anything."""
        q = GPUJobQueue()
        job = GPUJob(job_type=JobType.INFERENCE, clip_name="clip1", org_id="org-x")
        q.submit(job)
        claimed = q.claim_job("local", org_id=None)
        assert claimed is not None

    def test_shared_node_claims_any_org(self):
        """Shared nodes pass org_id=None and claim from any org."""
        q = GPUJobQueue()
        job_a = GPUJob(job_type=JobType.INFERENCE, clip_name="clip-a", org_id="org-a")
        job_b = GPUJob(job_type=JobType.INFERENCE, clip_name="clip-b", org_id="org-b")
        q.submit(job_a)
        q.submit(job_b)
        # Shared node passes org_id=None
        claimed1 = q.claim_job("shared-node", org_id=None)
        claimed2 = q.claim_job("shared-node", org_id=None)
        assert claimed1 is not None
        assert claimed2 is not None
