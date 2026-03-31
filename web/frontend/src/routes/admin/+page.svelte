<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';

	let stats = $state<any>(null);
	let pendingCount = $state(0);
	let userCount = $state(0);
	let orgCount = $state(0);
	let loading = $state(true);

	async function adminFetch(path: string) {
		const token = localStorage.getItem('ck:auth_token');
		const headers: Record<string, string> = { 'Content-Type': 'application/json' };
		if (token) headers['Authorization'] = `Bearer ${token}`;
		const res = await fetch(path, { headers });
		if (!res.ok) throw new Error('Failed');
		return res.json();
	}

	onMount(async () => {
		try {
			const [statsRes, usersRes, pendingRes, orgsRes] = await Promise.all([
				adminFetch('/api/admin/stats'),
				adminFetch('/api/admin/users'),
				adminFetch('/api/admin/users/pending'),
				adminFetch('/api/admin/orgs'),
			]);
			stats = statsRes;
			userCount = usersRes.users?.length ?? 0;
			pendingCount = pendingRes.users?.length ?? 0;
			orgCount = orgsRes.orgs?.length ?? 0;
		} catch { /* ignore */ }
		finally { loading = false; }
	});
</script>

<svelte:head>
	<title>Admin — CorridorKey</title>
</svelte:head>

{#if loading}
	<div class="loading mono">Loading...</div>
{:else}
	<!-- Quick stats cards -->
	<div class="stat-grid">
		<button class="stat-card" onclick={() => goto('/admin/users')}>
			<span class="stat-value">{pendingCount}</span>
			<span class="stat-label mono">PENDING APPROVAL</span>
			{#if pendingCount > 0}
				<span class="stat-alert"></span>
			{/if}
		</button>
		<button class="stat-card" onclick={() => goto('/admin/users')}>
			<span class="stat-value">{userCount}</span>
			<span class="stat-label mono">TOTAL USERS</span>
		</button>
		<div class="stat-card">
			<span class="stat-value">{orgCount}</span>
			<span class="stat-label mono">ORGANIZATIONS</span>
		</div>
		<div class="stat-card">
			<span class="stat-value">{stats?.nodes?.online ?? 0}<span class="stat-sub">/{stats?.nodes?.total ?? 0}</span></span>
			<span class="stat-label mono">NODES ONLINE</span>
		</div>
		<div class="stat-card">
			<span class="stat-value">{stats?.nodes?.gpus ?? 0}</span>
			<span class="stat-label mono">TOTAL GPUs</span>
		</div>
		<button class="stat-card" onclick={() => goto('/admin/system')}>
			<span class="stat-value mono" style="font-size: 14px">{stats?.queue?.running ?? 0} run / {stats?.queue?.queued ?? 0} queue</span>
			<span class="stat-label mono">JOB QUEUE</span>
		</button>
	</div>

	<!-- Platform stats breakdown -->
	{#if stats}
		<div class="section-grid">
			<div class="section-card">
				<h3 class="section-title mono">USERS BY TIER</h3>
				<div class="tier-list">
					{#each Object.entries(stats.users?.by_tier ?? {}) as [tier, count]}
						<div class="tier-row">
							<span class="tier-badge mono" data-tier={tier}>{tier}</span>
							<span class="tier-count mono">{count}</span>
						</div>
					{/each}
				</div>
			</div>
			<div class="section-card">
				<h3 class="section-title mono">GPU CREDITS</h3>
				<div class="credit-stats">
					<div class="credit-row">
						<span class="credit-label">Contributed</span>
						<span class="credit-value mono" style="color: var(--state-complete)">{((stats.credits?.total_contributed ?? 0) / 3600).toFixed(1)}h</span>
					</div>
					<div class="credit-row">
						<span class="credit-label">Consumed</span>
						<span class="credit-value mono" style="color: var(--state-error)">{((stats.credits?.total_consumed ?? 0) / 3600).toFixed(1)}h</span>
					</div>
				</div>
			</div>
			<div class="section-card">
				<h3 class="section-title mono">ORGANIZATIONS</h3>
				<div class="tier-list">
					<div class="tier-row">
						<span>Personal</span>
						<span class="tier-count mono">{stats.orgs?.personal ?? 0}</span>
					</div>
					<div class="tier-row">
						<span>Team</span>
						<span class="tier-count mono">{stats.orgs?.team ?? 0}</span>
					</div>
				</div>
			</div>
		</div>
	{/if}
{/if}

<style>
	.loading { text-align: center; padding: var(--sp-8); color: var(--text-tertiary); font-size: 12px; }

	.stat-grid {
		display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
		gap: var(--sp-3);
	}

	.stat-card {
		position: relative;
		background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-md);
		padding: var(--sp-4); display: flex; flex-direction: column; gap: var(--sp-1);
		text-align: left; font: inherit; color: inherit; cursor: default; transition: all 0.15s;
	}
	button.stat-card { cursor: pointer; }
	button.stat-card:hover { border-color: var(--accent); background: var(--surface-3); }

	.stat-value {
		font-family: var(--font-sans); font-size: 28px; font-weight: 700;
		color: var(--text-primary); letter-spacing: -0.02em; line-height: 1;
	}
	.stat-sub { font-size: 16px; color: var(--text-tertiary); font-weight: 400; }
	.stat-label { font-size: 9px; letter-spacing: 0.1em; color: var(--text-tertiary); }

	.stat-alert {
		position: absolute; top: 12px; right: 12px; width: 8px; height: 8px;
		border-radius: 50%; background: var(--accent);
		box-shadow: 0 0 8px var(--accent);
		animation: pulse 2s ease-in-out infinite;
	}
	@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

	.section-grid {
		display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
		gap: var(--sp-3);
	}

	.section-card {
		background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-md);
		padding: var(--sp-4); display: flex; flex-direction: column; gap: var(--sp-3);
	}

	.section-title {
		font-size: 10px; letter-spacing: 0.1em; color: var(--text-tertiary); font-weight: 600;
	}

	.tier-list { display: flex; flex-direction: column; gap: var(--sp-2); }
	.tier-row {
		display: flex; justify-content: space-between; align-items: center;
		font-size: 13px; color: var(--text-secondary);
	}
	.tier-count { color: var(--text-primary); font-weight: 600; }

	.tier-badge {
		font-size: 10px; padding: 2px 8px; border-radius: 3px; letter-spacing: 0.06em;
	}
	.tier-badge[data-tier="pending"] { background: rgba(255, 242, 3, 0.12); color: var(--accent); }
	.tier-badge[data-tier="member"] { background: rgba(61, 184, 255, 0.12); color: var(--state-ready); }
	.tier-badge[data-tier="contributor"] { background: rgba(93, 216, 121, 0.12); color: var(--state-complete); }
	.tier-badge[data-tier="org_admin"] { background: rgba(206, 147, 216, 0.12); color: var(--state-masked); }
	.tier-badge[data-tier="platform_admin"] { background: rgba(255, 82, 82, 0.12); color: var(--state-error); }
	.tier-badge[data-tier="rejected"] { background: rgba(117, 117, 117, 0.12); color: var(--state-cancelled); }

	.credit-stats { display: flex; flex-direction: column; gap: var(--sp-2); }
	.credit-row { display: flex; justify-content: space-between; font-size: 13px; color: var(--text-secondary); }
	.credit-value { font-weight: 600; }
</style>
