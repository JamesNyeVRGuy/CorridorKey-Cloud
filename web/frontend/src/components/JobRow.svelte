<script lang="ts">
	import type { Job } from '$lib/api';
	import { api } from '$lib/api';
	import { dismissJob } from '$lib/stores/jobs';
	import ProgressBar from './ProgressBar.svelte';

	import { refreshJobs } from '$lib/stores/jobs';

	let { job, showCancel = false, showDismiss = false, queueIndex = -1 }: { job: Job; showCancel?: boolean; showDismiss?: boolean; queueIndex?: number } = $props();

	function formatDuration(seconds: number): string {
		if (seconds <= 0) return '—';
		if (seconds < 60) return `${seconds.toFixed(1)}s`;
		const m = Math.floor(seconds / 60);
		const s = Math.round(seconds % 60);
		if (m < 60) return `${m}m ${s}s`;
		const h = Math.floor(m / 60);
		return `${h}h ${m % 60}m`;
	}
	let isQueued = $derived(job.status === 'queued');

	const typeLabels: Record<string, string> = {
		inference: 'Inference',
		gvm_alpha: 'GVM Alpha',
		videomama_alpha: 'VideoMaMa',
		preview_reprocess: 'Preview',
		video_extract: 'Extract',
		video_stitch: 'Stitch',
	};

	const statusColors: Record<string, string> = {
		running: 'var(--state-running)',
		queued: 'var(--state-queued)',
		completed: 'var(--state-complete)',
		cancelled: 'var(--state-cancelled)',
		failed: 'var(--state-failed)',
	};

	let label = $derived(typeLabels[job.job_type] ?? job.job_type);
	let statusColor = $derived(statusColors[job.status] ?? 'var(--text-tertiary)');
	let isRunning = $derived(job.status === 'running');
	let isFailed = $derived(job.status === 'failed');
	let expanded = $state(false);
	let logDetail = $state<string | null>(null);

	async function handleCancel() {
		if (job.shard_group && job.shard_total > 1) {
			await api.jobs.cancelShardGroup(job.shard_group);
		} else {
			await api.jobs.cancel(job.id);
		}
		refreshJobs();
	}

	async function handleRetryShards() {
		if (!job.shard_group) return;
		await api.jobs.retryShardGroup(job.shard_group);
		refreshJobs();
	}

	async function moveUp() {
		if (queueIndex > 0) {
			await api.jobs.move(job.id, queueIndex - 1);
			refreshJobs();
		}
	}

	async function moveDown() {
		await api.jobs.move(job.id, queueIndex + 1);
		refreshJobs();
	}

	async function toggleLog() {
		if (!isFailed) return;
		expanded = !expanded;
		if (expanded && !logDetail) {
			try {
				const log = await api.jobs.getLog(job.id);
				logDetail = JSON.stringify(log, null, 2);
			} catch {
				logDetail = job.error_message ?? 'No details available';
			}
		}
	}
</script>

<div class="job-row" class:running={isRunning} class:failed={isFailed} role="button" tabindex="0" onclick={toggleLog} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleLog(); }}}>
	<div class="job-type mono">
		<span class="type-dot" style="background: {statusColor}; box-shadow: 0 0 6px {statusColor}"></span>
		{label}
		{#if job.shard_total > 1}
			<span class="shard-badge">{job.shard_index + 1}/{job.shard_total}</span>
		{/if}
	</div>

	<div class="job-info">
		<span class="job-clip">{job.clip_name}</span>
		{#if isRunning}
			<ProgressBar current={job.current_frame} total={job.total_frames} startedAt={job.started_at > 0 ? job.started_at * 1000 : null} uploadPass={job.upload_pass} />
			<div class="job-stats mono">
				<span>{job.current_frame}/{job.total_frames} frames</span>
				<span class="stat-sep">&bull;</span>
				<span>{job.fps} fps</span>
				<span class="stat-sep">&bull;</span>
				<span>{formatDuration(job.duration_seconds)}</span>
			</div>
		{:else if job.status === 'completed'}
			<div class="job-stats mono">
				<span class="job-status" style="color: {statusColor}">COMPLETED</span>
				<span class="stat-sep">&bull;</span>
				<span>{job.total_frames} frames</span>
				<span class="stat-sep">&bull;</span>
				<span>{job.fps} fps</span>
				<span class="stat-sep">&bull;</span>
				<span>{formatDuration(job.duration_seconds)}</span>
			</div>
		{:else if isQueued && job.queue_position}
			<span class="job-status mono" style="color: {statusColor}">
				QUEUED #{job.queue_position}
				{#if job.estimated_wait_seconds}
					<span class="queue-eta">~{formatDuration(job.estimated_wait_seconds)}</span>
				{/if}
			</span>
		{:else}
			<span class="job-status mono" style="color: {statusColor}">{job.status.toUpperCase()}</span>
		{/if}
	</div>

	<div class="job-actions">
		{#if job.claimed_by}
			<span class="job-node mono" title="Processed by {job.claimed_by}">{job.claimed_by}</span>
		{/if}
		<span class="job-id mono">{job.id}</span>
		{#if isQueued && queueIndex >= 0}
			<button class="move-btn" onclick={moveUp} title="Move up" disabled={queueIndex === 0}>
				<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M6 2.5L2.5 6.5h7L6 2.5z" fill="currentColor"/></svg>
			</button>
			<button class="move-btn" onclick={moveDown} title="Move down">
				<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M6 9.5L2.5 5.5h7L6 9.5z" fill="currentColor"/></svg>
			</button>
		{/if}
		{#if isFailed && job.shard_group && job.shard_total > 1}
			<button class="retry-btn" onclick={handleRetryShards} title="Retry failed shards">
				<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M12 7a5 5 0 11-1.5-3.5M12 2v3h-3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
			</button>
		{/if}
		{#if showCancel && (job.status === 'running' || job.status === 'queued')}
			<button class="cancel-btn" onclick={handleCancel} title={job.shard_total > 1 ? 'Cancel all shards' : 'Cancel job'}>
				<svg width="14" height="14" viewBox="0 0 14 14" fill="none">
					<path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
				</svg>
			</button>
		{/if}
		{#if showDismiss && !isRunning && !isQueued}
			<button class="dismiss-btn" onclick={() => dismissJob(job.id)} title="Remove from history">
				<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
			</button>
		{/if}
	</div>

	{#if job.error_message}
		<div class="job-error mono">
			{job.error_message}
			{#if isFailed}
				<span class="expand-hint">{expanded ? '▲ collapse' : '▼ click for details'}</span>
			{/if}
		</div>
	{/if}
	{#if expanded && logDetail}
		<pre class="job-log mono">{logDetail}</pre>
	{/if}
</div>

<style>
	.job-row {
		display: grid;
		grid-template-columns: 110px 1fr auto;
		gap: var(--sp-3);
		align-items: center;
		padding: var(--sp-3) var(--sp-4);
		border-bottom: 1px solid var(--border-subtle);
		transition: background 0.15s;
	}

	.job-row:last-child {
		border-bottom: none;
	}

	.job-row:hover {
		background: var(--surface-2);
	}

	.job-row.running {
		background: linear-gradient(90deg, rgba(255, 242, 3, 0.06), transparent);
		border-left: 3px solid var(--accent);
		box-shadow: inset 0 0 20px rgba(255, 242, 3, 0.02);
	}

	.job-type {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		font-size: 11px;
		font-weight: 500;
		color: var(--text-secondary);
	}

	.type-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.job-info {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-width: 0;
	}

	.job-clip {
		font-size: 13px;
		font-weight: 600;
		color: var(--text-primary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.job-status {
		font-size: 10px;
		letter-spacing: 0.06em;
		font-weight: 500;
	}
	.queue-eta {
		color: var(--text-tertiary);
		margin-left: 4px;
	}

	.job-stats {
		display: flex;
		align-items: center;
		gap: var(--sp-1);
		font-size: 10px;
		color: var(--text-tertiary);
		flex-wrap: wrap;
	}

	.stat-sep {
		opacity: 0.3;
		font-size: 8px;
	}

	.job-actions {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.shard-badge {
		font-size: 8px;
		padding: 1px 4px;
		background: var(--secondary-muted);
		color: var(--secondary);
		border-radius: 3px;
		font-weight: 600;
	}

	.job-node {
		font-size: 9px;
		color: var(--secondary);
		padding: 1px 5px;
		border: 1px solid var(--secondary-muted);
		border-radius: 3px;
		white-space: nowrap;
	}

	.job-id {
		font-size: 9px;
		color: var(--text-tertiary);
	}

	.move-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: 3px;
		border: 1px solid var(--border);
		background: var(--surface-3);
		color: var(--text-tertiary);
		cursor: pointer;
		transition: all 0.15s;
		padding: 0;
	}

	.move-btn:hover:not(:disabled) {
		color: var(--text-primary);
		border-color: var(--text-tertiary);
	}

	.move-btn:disabled {
		opacity: 0.3;
		cursor: default;
	}

	.retry-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--surface-3);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.15s;
	}

	.retry-btn:hover {
		color: var(--state-complete);
		border-color: var(--state-complete);
		background: rgba(93, 216, 121, 0.1);
	}

	.cancel-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--surface-3);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.15s;
	}

	.cancel-btn:hover {
		color: var(--state-error);
		border-color: var(--state-error);
		background: rgba(255, 82, 82, 0.1);
		box-shadow: 0 0 8px rgba(255, 82, 82, 0.1);
	}

	.dismiss-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: 3px;
		border: none;
		background: transparent;
		color: var(--text-tertiary);
		cursor: pointer;
		transition: all 0.15s;
		padding: 0;
		opacity: 0;
	}

	.job-row:hover .dismiss-btn { opacity: 1; }
	.dismiss-btn:hover { color: var(--text-secondary); }

	.job-row.failed {
		cursor: pointer;
	}

	.job-error {
		grid-column: 1 / -1;
		font-size: 11px;
		color: var(--state-error);
		padding: var(--sp-1) 0 0 calc(110px + var(--sp-3));
		display: flex;
		align-items: baseline;
		gap: var(--sp-2);
	}

	.expand-hint {
		font-size: 9px;
		color: var(--text-tertiary);
		flex-shrink: 0;
	}

	.job-log {
		grid-column: 1 / -1;
		font-size: 10px;
		color: var(--text-secondary);
		background: var(--surface-1);
		padding: var(--sp-3);
		border-radius: var(--radius-sm);
		max-height: 200px;
		overflow: auto;
		white-space: pre-wrap;
		word-break: break-all;
		margin: var(--sp-2) 0 0 calc(110px + var(--sp-3));
	}
</style>
