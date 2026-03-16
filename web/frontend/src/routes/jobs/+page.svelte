<script lang="ts">
	import { currentJob, runningJobs, queuedJobs, jobHistory, refreshJobs } from '$lib/stores/jobs';
	import { api } from '$lib/api';
	import type { Job } from '$lib/api';
	import JobRow from '../../components/JobRow.svelte';
	import ProgressBar from '../../components/ProgressBar.svelte';

	let cancelling = $state(false);
	let expandedGroups = $state<Set<string>>(new Set());

	async function cancelAll() {
		cancelling = true;
		try {
			await api.jobs.cancelAll();
			await refreshJobs();
		} finally {
			cancelling = false;
		}
	}

	function toggleGroup(groupId: string) {
		const next = new Set(expandedGroups);
		if (next.has(groupId)) next.delete(groupId);
		else next.add(groupId);
		expandedGroups = next;
	}

	interface ShardGroup {
		group_id: string;
		clip_name: string;
		shards: Job[];
		current_frame: number;
		total_frames: number;
		completed: number;
		running: number;
		failed: number;
	}

	function groupShards(jobs: Job[]): (Job | ShardGroup)[] {
		const groups = new Map<string, Job[]>();
		const singles: Job[] = [];

		for (const job of jobs) {
			if (job.shard_group && job.shard_total > 1) {
				const list = groups.get(job.shard_group) ?? [];
				list.push(job);
				groups.set(job.shard_group, list);
			} else {
				singles.push(job);
			}
		}

		const result: (Job | ShardGroup)[] = [];
		for (const [group_id, shards] of groups) {
			result.push({
				group_id,
				clip_name: shards[0].clip_name,
				shards: shards.sort((a, b) => a.shard_index - b.shard_index),
				current_frame: shards.reduce((s, j) => s + j.current_frame, 0),
				total_frames: shards.reduce((s, j) => s + j.total_frames, 0),
				completed: shards.filter((j) => j.status === 'completed').length,
				running: shards.filter((j) => j.status === 'running').length,
				failed: shards.filter((j) => j.status === 'failed').length,
			});
		}
		result.push(...singles);
		return result;
	}

	function isShardGroup(item: Job | ShardGroup): item is ShardGroup {
		return 'group_id' in item;
	}

	let groupedRunning = $derived(groupShards($runningJobs));
	let groupedQueued = $derived(groupShards($queuedJobs));
	let groupedHistory = $derived(groupShards($jobHistory));

	let hasActive = $derived($runningJobs.length > 0 || $queuedJobs.length > 0);
</script>

<svelte:head>
	<title>Jobs — CorridorKey</title>
</svelte:head>

<div class="page">
	<div class="page-header">
		<h1 class="page-title">Jobs</h1>
		<div class="header-actions">
			<button class="btn-ghost" onclick={() => refreshJobs()}>
				<svg width="14" height="14" viewBox="0 0 14 14" fill="none">
					<path d="M12 7a5 5 0 11-1.5-3.5M12 2v3h-3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
				</svg>
				Refresh
			</button>
			{#if hasActive}
				<button class="btn-ghost btn-danger" onclick={cancelAll} disabled={cancelling}>
					Cancel All
				</button>
			{/if}
		</div>
	</div>

	<!-- Running Jobs -->
	{#if $runningJobs.length > 0}
		<section class="section">
			<h2 class="section-title mono">RUNNING <span class="count">{$runningJobs.length}</span></h2>
			<div class="job-list">
				{#each groupedRunning as item}
					{#if isShardGroup(item)}
						{@const g = item}
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<div class="shard-group" onclick={() => toggleGroup(g.group_id)}>
							<div class="shard-group-header">
								<span class="type-dot" style="background: var(--state-running); box-shadow: 0 0 6px var(--state-running)"></span>
								<span class="shard-group-label mono">SHARDED</span>
								<span class="shard-group-clip">{g.clip_name}</span>
								<span class="shard-group-info mono">{g.completed}/{g.shards.length} GPUs done</span>
								<span class="shard-group-expand mono">{expandedGroups.has(g.group_id) ? '▲' : '▼'}</span>
							</div>
							<ProgressBar current={g.current_frame} total={g.total_frames} />
						</div>
						{#if expandedGroups.has(g.group_id)}
							{#each g.shards as job (job.id)}
								<JobRow {job} showCancel />
							{/each}
						{/if}
					{:else}
						<JobRow job={item} showCancel />
					{/if}
				{/each}
			</div>
		</section>
	{/if}

	<!-- Queued -->
	{#if $queuedJobs.length > 0}
		<section class="section">
			<h2 class="section-title mono">QUEUED <span class="count">{$queuedJobs.length}</span></h2>
			<div class="job-list">
				{#each groupedQueued as item, i}
					{#if isShardGroup(item)}
						{@const g = item}
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<div class="shard-group" onclick={() => toggleGroup(g.group_id)}>
							<div class="shard-group-header">
								<span class="type-dot" style="background: var(--state-queued)"></span>
								<span class="shard-group-label mono">SHARDED</span>
								<span class="shard-group-clip">{g.clip_name}</span>
								<span class="shard-group-info mono">{g.shards.length} shards queued</span>
								<span class="shard-group-expand mono">{expandedGroups.has(g.group_id) ? '▲' : '▼'}</span>
							</div>
						</div>
						{#if expandedGroups.has(g.group_id)}
							{#each g.shards as job, j (job.id)}
								<JobRow {job} showCancel queueIndex={j} />
							{/each}
						{/if}
					{:else}
						<JobRow job={item} showCancel queueIndex={i} />
					{/if}
				{/each}
			</div>
		</section>
	{/if}

	<!-- History -->
	{#if $jobHistory.length > 0}
		<section class="section">
			<h2 class="section-title mono">HISTORY</h2>
			<div class="job-list">
				{#each groupedHistory as item}
					{#if isShardGroup(item)}
						{@const g = item}
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<div class="shard-group" onclick={() => toggleGroup(g.group_id)}>
							<div class="shard-group-header">
								<span class="type-dot" style="background: {g.failed > 0 ? 'var(--state-failed)' : 'var(--state-complete)'}"></span>
								<span class="shard-group-label mono">SHARDED</span>
								<span class="shard-group-clip">{g.clip_name}</span>
								<span class="shard-group-info mono">{g.completed}/{g.shards.length} done{g.failed > 0 ? `, ${g.failed} failed` : ''}</span>
								<span class="shard-group-expand mono">{expandedGroups.has(g.group_id) ? '▲' : '▼'}</span>
							</div>
						</div>
						{#if expandedGroups.has(g.group_id)}
							{#each g.shards as job (job.id)}
								<JobRow {job} />
							{/each}
						{/if}
					{:else}
						<JobRow job={item} />
					{/if}
				{/each}
			</div>
		</section>
	{/if}

	{#if $runningJobs.length === 0 && $queuedJobs.length === 0 && $jobHistory.length === 0}
		<div class="empty-state">
			<svg width="48" height="48" viewBox="0 0 48 48" fill="none">
				<path d="M8 16l16 9.5L40 16M8 24l16 9.5L40 24M8 32l16 9.5L40 32M8 8L24 17.5 40 8 24 0 8 8z" stroke="var(--text-tertiary)" stroke-width="1.5" stroke-linejoin="round"/>
			</svg>
			<p class="empty-text">No jobs</p>
			<p class="empty-hint mono">Submit a job from a clip's detail page.</p>
		</div>
	{/if}
</div>

<style>
	.page {
		padding: var(--sp-5) var(--sp-6);
		display: flex;
		flex-direction: column;
		gap: var(--sp-4);
	}

	.page-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.page-title {
		font-family: var(--font-sans);
		font-size: 20px;
		font-weight: 700;
		letter-spacing: -0.01em;
	}

	.header-actions {
		display: flex;
		gap: var(--sp-2);
	}

	.btn-ghost {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		padding: var(--sp-2) var(--sp-3);
		font-size: 12px;
		font-weight: 500;
		color: var(--text-secondary);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 6px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.btn-ghost:hover {
		color: var(--text-primary);
		border-color: var(--text-tertiary);
		background: var(--surface-2);
	}

	.btn-ghost:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-danger {
		color: var(--state-error);
		border-color: rgba(255, 82, 82, 0.3);
	}

	.btn-danger:hover {
		color: var(--state-error) !important;
		background: rgba(255, 82, 82, 0.08) !important;
		border-color: rgba(255, 82, 82, 0.5) !important;
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: 0;
	}

	.section-title {
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.1em;
		color: var(--text-tertiary);
		padding: var(--sp-2) var(--sp-4);
		background: var(--surface-1);
		border: 1px solid var(--border);
		border-radius: 8px 8px 0 0;
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.count {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		font-size: 9px;
		background: var(--surface-4);
		border-radius: 8px;
		color: var(--text-secondary);
	}

	.job-list {
		border: 1px solid var(--border);
		border-top: none;
		border-radius: 0 0 8px 8px;
		overflow: hidden;
		background: var(--surface-1);
	}

	.shard-group {
		padding: var(--sp-3) var(--sp-4);
		border-bottom: 1px solid var(--border-subtle);
		cursor: pointer;
		transition: background 0.15s;
		background: linear-gradient(90deg, rgba(0, 154, 218, 0.04), transparent);
		border-left: 3px solid var(--secondary);
	}

	.shard-group:hover {
		background: var(--surface-2);
	}

	.shard-group-header {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		margin-bottom: var(--sp-2);
	}

	.type-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.shard-group-label {
		font-size: 9px;
		font-weight: 600;
		color: var(--secondary);
		padding: 1px 5px;
		border: 1px solid var(--secondary-muted);
		border-radius: 3px;
		letter-spacing: 0.06em;
	}

	.shard-group-clip {
		font-size: 13px;
		font-weight: 600;
		color: var(--text-primary);
	}

	.shard-group-info {
		margin-left: auto;
		font-size: 10px;
		color: var(--text-secondary);
	}

	.shard-group-expand {
		font-size: 9px;
		color: var(--text-tertiary);
		margin-left: var(--sp-1);
	}

	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--sp-3);
		padding: var(--sp-8) 0;
		text-align: center;
	}

	.empty-text {
		font-size: 15px;
		font-weight: 500;
		color: var(--text-secondary);
	}

	.empty-hint {
		font-size: 11px;
		color: var(--text-tertiary);
		max-width: 300px;
	}
</style>
