<script lang="ts">
	import { onMount } from 'svelte';

	let entries = $state<{ id: number; timestamp: string; actor_user_id: string; action: string; target_type: string; target_id: string; details: any; ip_address: string }[]>([]);
	let total = $state(0);
	let currentPage = $state(0);
	let filter = $state('');
	let loading = $state(true);

	// User lookup for display names
	let userLookup = $state<Map<string, { name: string; email: string }>>(new Map());

	async function adminFetch(path: string) {
		const token = localStorage.getItem('ck:auth_token');
		const headers: Record<string, string> = { 'Content-Type': 'application/json' };
		if (token) headers['Authorization'] = `Bearer ${token}`;
		const res = await fetch(path, { headers });
		if (!res.ok) throw new Error('Failed');
		return res.json();
	}

	async function loadAudit() {
		const params = new URLSearchParams({ limit: '50', offset: String(currentPage * 50) });
		if (filter) params.set('action', filter);
		const res = await adminFetch(`/api/admin/audit?${params}`);
		entries = res.entries;
		total = res.total;
	}

	async function loadUsers() {
		try {
			const res = await adminFetch('/api/admin/users');
			const map = new Map<string, { name: string; email: string }>();
			for (const u of res.users) map.set(u.user_id, { name: u.name, email: u.email });
			userLookup = map;
		} catch { /* ignore */ }
	}

	function resolveUser(id: string): string {
		if (!id) return '—';
		const u = userLookup.get(id);
		if (u) return u.name?.trim() || u.email;
		return id.substring(0, 12) + '...';
	}

	function formatTime(ts: string): string {
		if (!ts) return '—';
		const d = new Date(ts);
		return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
	}

	onMount(async () => {
		try { await Promise.all([loadAudit(), loadUsers()]); }
		catch { /* ignore */ }
		finally { loading = false; }
	});
</script>

<svelte:head>
	<title>Audit Log — Admin — CorridorKey</title>
</svelte:head>

{#if loading}
	<div class="loading mono">Loading audit log...</div>
{:else}
	<div class="audit-controls">
		<select class="filter-select mono" bind:value={filter} onchange={() => { currentPage = 0; loadAudit(); }}>
			<option value="">All actions</option>
			<option value="user.approve">User approved</option>
			<option value="user.reject">User rejected</option>
			<option value="user.tier_change">Tier change</option>
			<option value="credit.grant">Credit grant</option>
			<option value="credit.revoke">Credit revoke</option>
		</select>
		<span class="audit-total mono">{total} entries</span>
	</div>

	<div class="audit-list">
		<div class="audit-header">
			<span class="col-time mono">TIME</span>
			<span class="col-actor mono">ACTOR</span>
			<span class="col-action mono">ACTION</span>
			<span class="col-target mono">TARGET</span>
			<span class="col-detail mono">DETAILS</span>
		</div>
		{#each entries as entry (entry.id)}
			<div class="audit-row">
				<span class="col-time mono">{formatTime(entry.timestamp)}</span>
				<span class="col-actor">{resolveUser(entry.actor_user_id)}</span>
				<span class="col-action mono">{entry.action}</span>
				<span class="col-target mono">{entry.target_type ? `${entry.target_type}:${entry.target_id?.substring(0, 8) ?? ''}` : '—'}</span>
				<span class="col-detail mono">{entry.details ? JSON.stringify(entry.details) : '—'}</span>
			</div>
		{/each}
	</div>

	{#if total > 50}
		<div class="pagination">
			<button class="btn-page mono" disabled={currentPage === 0} onclick={() => { currentPage--; loadAudit(); }}>PREV</button>
			<span class="mono page-info">{currentPage * 50 + 1}–{Math.min((currentPage + 1) * 50, total)} of {total}</span>
			<button class="btn-page mono" disabled={(currentPage + 1) * 50 >= total} onclick={() => { currentPage++; loadAudit(); }}>NEXT</button>
		</div>
	{/if}
{/if}

<style>
	.loading { text-align: center; padding: var(--sp-8); color: var(--text-tertiary); font-size: 12px; }

	.audit-controls { display: flex; align-items: center; gap: var(--sp-2); }
	.filter-select {
		padding: 7px 10px; background: var(--surface-2); border: 1px solid var(--border);
		border-radius: 6px; color: var(--text-primary); font-size: 12px;
	}
	.audit-total { font-size: 10px; color: var(--text-tertiary); margin-left: auto; }

	.audit-list {
		border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden;
		background: var(--surface-1);
	}

	.audit-header {
		display: grid; grid-template-columns: 120px 140px 140px 140px 1fr;
		gap: var(--sp-2); padding: var(--sp-2) var(--sp-3);
		background: var(--surface-2); border-bottom: 1px solid var(--border);
		font-size: 9px; letter-spacing: 0.08em; color: var(--text-tertiary); font-weight: 600;
	}

	.audit-row {
		display: grid; grid-template-columns: 120px 140px 140px 140px 1fr;
		gap: var(--sp-2); padding: var(--sp-2) var(--sp-3);
		border-bottom: 1px solid var(--border-subtle); font-size: 12px; color: var(--text-secondary);
	}
	.audit-row:last-child { border-bottom: none; }
	.audit-row:hover { background: var(--surface-2); }

	.col-time { font-size: 11px; color: var(--text-tertiary); }
	.col-actor { font-size: 12px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.col-action { font-size: 11px; color: var(--secondary); }
	.col-target { font-size: 11px; color: var(--text-tertiary); }
	.col-detail { font-size: 10px; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

	.pagination { display: flex; align-items: center; justify-content: center; gap: var(--sp-3); }
	.page-info { font-size: 11px; color: var(--text-tertiary); }
	.btn-page {
		padding: 4px 10px; font-size: 10px; letter-spacing: 0.06em;
		background: var(--surface-3); border: 1px solid var(--border); border-radius: var(--radius-sm);
		color: var(--text-secondary); cursor: pointer; transition: all 0.15s;
	}
	.btn-page:hover:not(:disabled) { border-color: var(--accent); color: var(--text-primary); }
	.btn-page:disabled { opacity: 0.3; cursor: not-allowed; }
</style>
