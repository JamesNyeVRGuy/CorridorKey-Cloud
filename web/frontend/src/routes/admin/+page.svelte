<script lang="ts">
	import { onMount } from 'svelte';
	import { getStoredUser } from '$lib/auth';

	interface UserRecord {
		user_id: string;
		email: string;
		tier: string;
		name: string;
		signed_up_at: number;
		approved_at: number;
		approved_by: string;
		orgs?: { org_id: string; name: string }[];
	}

	interface OrgRecord {
		org_id: string;
		name: string;
		owner_id: string;
		personal: boolean;
		created_at: number;
		member_count: number;
	}

	const TIERS = ['pending', 'member', 'contributor', 'org_admin', 'platform_admin'];

	let authorized = $state(false);
	let activeTab = $state<'users' | 'orgs' | 'credits' | 'stats' | 'audit'>('users');
	let users = $state<UserRecord[]>([]);
	let pendingUsers = $state<UserRecord[]>([]);
	let orgs = $state<OrgRecord[]>([]);
	let loading = $state(true);
	let actionInProgress = $state<string | null>(null);
	let inviteUrl = $state('');
	let inviteGenerating = $state(false);
	let invites = $state<{ token: string; created_at: number; used: boolean; used_by: string | null }[]>([]);

	// Credits
	let allCredits = $state<{ org_id: string; org_name: string; contributed_hours: number; consumed_hours: number; balance_seconds: number }[]>([]);
	let grantOrgId = $state('');
	let grantHours = $state(1);
	let granting = $state(false);

	// Stats
	let stats = $state<any>(null);

	// User activity detail
	let selectedUserActivity = $state<any>(null);
	let activityLoading = $state(false);

	// Audit log
	let auditEntries = $state<{ id: number; timestamp: string; actor_user_id: string; action: string; target_type: string; target_id: string; details: any; ip_address: string }[]>([]);
	let auditTotal = $state(0);
	let auditPage = $state(0);
	let auditFilter = $state('');

	// User ID → display name lookup (built from users list)
	let userLookup = $derived.by(() => {
		const map = new Map<string, { name: string; email: string }>();
		for (const u of users) {
			map.set(u.user_id, { name: u.name, email: u.email });
		}
		return map;
	});

	function displayName(u: { name?: string; email: string }): string {
		return u.name?.trim() || u.email;
	}

	function resolveUser(id: string): string {
		if (!id) return '—';
		const u = userLookup.get(id);
		if (u) return u.name?.trim() || u.email;
		return id.substring(0, 12) + '...';
	}

	async function adminFetch(path: string, opts?: RequestInit) {
		const token = localStorage.getItem('ck:auth_token');
		const headers: Record<string, string> = { 'Content-Type': 'application/json' };
		if (token) headers['Authorization'] = `Bearer ${token}`;
		const res = await fetch(path, { ...opts, headers });
		if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
		return res.json();
	}

	async function loadUsers() {
		const [allRes, pendingRes] = await Promise.all([
			adminFetch('/api/admin/users'),
			adminFetch('/api/admin/users/pending')
		]);
		users = allRes.users;
		pendingUsers = pendingRes.users;
	}

	let inviteError = $state('');

	async function generateInvite() {
		inviteGenerating = true;
		inviteUrl = '';
		inviteError = '';
		try {
			const res = await adminFetch('/api/auth/invite/generate', { method: 'POST' });
			inviteUrl = `${window.location.origin}${res.signup_url}`;
			await loadInvites();
		} catch (e) {
			inviteError = e instanceof Error ? e.message : 'Failed to generate invite';
		} finally {
			inviteGenerating = false;
		}
	}

	async function copyInvite() {
		await navigator.clipboard.writeText(inviteUrl);
	}

	async function loadInvites() {
		try {
			const res = await adminFetch('/api/auth/invites');
			invites = res.invites;
		} catch {
			invites = [];
		}
	}

	async function loadCredits() {
		try {
			const res = await adminFetch('/api/admin/credits');
			allCredits = res.credits;
		} catch { allCredits = []; }
	}

	async function loadStats() {
		try {
			stats = await adminFetch('/api/admin/stats');
		} catch { stats = null; }
	}

	async function grantCredits() {
		if (!grantOrgId || grantHours === 0) return;
		granting = true;
		try {
			await adminFetch('/api/admin/credits/grant', {
				method: 'POST',
				body: JSON.stringify({ org_id: grantOrgId, hours: grantHours })
			});
			await loadCredits();
			grantHours = 1;
		} catch { /* ignore */ }
		finally { granting = false; }
	}

	async function viewUserActivity(userId: string) {
		activityLoading = true;
		selectedUserActivity = null;
		try {
			selectedUserActivity = await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/activity`);
		} catch { /* ignore */ }
		finally { activityLoading = false; }
	}

	async function loadAudit() {
		try {
			const params = new URLSearchParams({ limit: '50', offset: String(auditPage * 50) });
			if (auditFilter) params.set('action', auditFilter);
			const res = await adminFetch(`/api/admin/audit?${params}`);
			auditEntries = res.entries;
			auditTotal = res.total;
		} catch { auditEntries = []; }
	}

	async function loadOrgs() {
		const res = await adminFetch('/api/admin/orgs');
		orgs = res.orgs;
	}

	async function approveUser(userId: string) {
		actionInProgress = userId;
		try {
			await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/approve`, { method: 'POST' });
			await loadUsers();
		} finally {
			actionInProgress = null;
		}
	}

	async function rejectUser(userId: string) {
		actionInProgress = userId;
		try {
			await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/reject`, { method: 'POST' });
			await loadUsers();
		} finally {
			actionInProgress = null;
		}
	}

	async function setTier(userId: string, tier: string) {
		actionInProgress = userId;
		try {
			await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/tier`, {
				method: 'POST',
				body: JSON.stringify({ tier })
			});
			await loadUsers();
		} finally {
			actionInProgress = null;
		}
	}

	function formatDate(ts: number): string {
		if (!ts) return '—';
		return new Date(ts * 1000).toLocaleDateString('en-US', {
			month: 'short', day: 'numeric', year: 'numeric'
		});
	}

	onMount(async () => {
		const user = getStoredUser();
		if (user?.tier !== 'platform_admin') {
			authorized = false;
			loading = false;
			return;
		}
		authorized = true;
		try {
			await Promise.all([loadUsers(), loadOrgs(), loadInvites(), loadCredits(), loadStats(), loadAudit()]);
		} finally {
			loading = false;
		}
	});
</script>

<svelte:head>
	<title>Admin — CorridorKey</title>
</svelte:head>

<div class="admin-page">
	{#if !authorized && !loading}
		<div class="denied">
			<span class="denied-icon">ACCESS DENIED</span>
			<p>This page requires platform_admin privileges.</p>
		</div>
	{:else if loading}
		<div class="loading mono">Loading...</div>
	{:else}
		<div class="admin-header">
			<h1 class="page-title mono">ADMIN</h1>
			<div class="tab-bar">
				<button
					class="tab-btn mono"
					class:active={activeTab === 'users'}
					onclick={() => activeTab = 'users'}
				>
					USERS
					{#if pendingUsers.length > 0}
						<span class="tab-badge">{pendingUsers.length}</span>
					{/if}
				</button>
				<button
					class="tab-btn mono"
					class:active={activeTab === 'orgs'}
					onclick={() => activeTab = 'orgs'}
				>ORGS</button>
				<button
					class="tab-btn mono"
					class:active={activeTab === 'credits'}
					onclick={() => activeTab = 'credits'}
				>CREDITS</button>
				<button
					class="tab-btn mono"
					class:active={activeTab === 'stats'}
					onclick={() => activeTab = 'stats'}
				>STATS</button>
				<button
					class="tab-btn mono"
					class:active={activeTab === 'audit'}
					onclick={() => activeTab = 'audit'}
				>AUDIT</button>
			</div>
		</div>

		{#if activeTab === 'users'}
			<!-- Invite Generation -->
			<div class="section">
				<h2 class="section-title mono">INVITE LINK</h2>
				<div class="invite-row">
					<button class="btn btn-primary mono" onclick={generateInvite} disabled={inviteGenerating}>
						{inviteGenerating ? 'Generating...' : 'Generate Invite Link'}
					</button>
					{#if inviteError}
						<div class="form-error mono">{inviteError}</div>
					{/if}
					{#if inviteUrl}
						<div class="invite-result">
							<input type="text" class="invite-url mono" value={inviteUrl} readonly />
							<button class="btn btn-copy mono" onclick={copyInvite}>COPY</button>
						</div>
					{/if}
				</div>
				{#if invites.length > 0}
					<div class="invite-list">
						<table class="data-table">
							<thead>
								<tr>
									<th class="mono">TOKEN</th>
									<th class="mono">STATUS</th>
									<th class="mono">USED BY</th>
									<th class="mono">CREATED</th>
								</tr>
							</thead>
							<tbody>
								{#each invites as inv}
									<tr>
										<td class="mono">{inv.token}</td>
										<td>
											{#if inv.used}
												<span class="status-badge used mono">USED</span>
											{:else}
												<span class="status-badge available mono">AVAILABLE</span>
											{/if}
										</td>
										<td class="mono">{inv.used_by ?? '—'}</td>
										<td class="mono">{formatDate(inv.created_at)}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</div>

			<!-- Pending Approvals -->
			{#if pendingUsers.length > 0}
				<div class="section">
					<h2 class="section-title mono">PENDING APPROVAL</h2>
					<div class="pending-list">
						{#each pendingUsers as pu (pu.user_id)}
							<div class="pending-card">
								<div class="pending-info">
									<span class="pending-email">{displayName(pu)}</span>
									{#if pu.name}
										<span class="pending-name mono">{pu.email}</span>
									{/if}
									{#if pu.company || pu.role}
										<span class="pending-profile mono">
											{[pu.company, pu.role].filter(Boolean).join(' · ')}
										</span>
									{/if}
									{#if pu.use_case}
										<span class="pending-usecase">{pu.use_case}</span>
									{/if}
									<span class="pending-date mono">{formatDate(pu.signed_up_at)}</span>
								</div>
								<div class="pending-actions">
									<button
										class="btn btn-approve mono"
										onclick={() => approveUser(pu.user_id)}
										disabled={actionInProgress === pu.user_id}
									>APPROVE</button>
									<button
										class="btn btn-reject mono"
										onclick={() => rejectUser(pu.user_id)}
										disabled={actionInProgress === pu.user_id}
									>REJECT</button>
								</div>
							</div>
						{/each}
					</div>
				</div>
			{/if}

			<!-- All Users -->
			<div class="section">
				<h2 class="section-title mono">ALL USERS <span class="count">{users.length}</span></h2>
				<div class="table-wrap">
					<table class="data-table">
						<thead>
							<tr>
								<th class="mono">USER</th>
								<th class="mono">TIER</th>
								<th class="mono">ORGS</th>
								<th class="mono">SIGNED UP</th>
								<th class="mono">ACTIONS</th>
							</tr>
						</thead>
						<tbody>
							{#each users as u (u.user_id)}
								<tr>
									<td>
										<span class="user-email">{displayName(u)}</span>
										{#if u.name}<span class="user-name mono">{u.email}</span>{/if}
									</td>
									<td>
										<span class="tier-badge mono" data-tier={u.tier}>{u.tier}</span>
									</td>
									<td class="mono org-cell">
										{#if u.orgs && u.orgs.length > 0}
											{#each u.orgs as o}
												<span class="org-chip">{o.name}</span>
											{/each}
										{:else}
											<span class="no-org">—</span>
										{/if}
									</td>
									<td class="mono">{formatDate(u.signed_up_at)}</td>
									<td>
										<select
											class="tier-select mono"
											value={u.tier}
											onchange={(e) => setTier(u.user_id, (e.target as HTMLSelectElement).value)}
											disabled={actionInProgress === u.user_id}
										>
											{#each TIERS as t}
												<option value={t}>{t}</option>
											{/each}
										</select>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>
		{:else if activeTab === 'orgs'}
			<!-- Organizations -->
			<div class="section">
				<h2 class="section-title mono">ALL ORGANIZATIONS <span class="count">{orgs.length}</span></h2>
				<div class="table-wrap">
					<table class="data-table">
						<thead>
							<tr>
								<th class="mono">NAME</th>
								<th class="mono">OWNER</th>
								<th class="mono">MEMBERS</th>
								<th class="mono">TYPE</th>
								<th class="mono">CREATED</th>
							</tr>
						</thead>
						<tbody>
							{#each orgs as org (org.org_id)}
								<tr>
									<td>{org.name}</td>
									<td class="mono">{resolveUser(org.owner_id)}</td>
									<td class="mono">{org.member_count}</td>
									<td>
										{#if org.personal}
											<span class="org-type mono personal">PERSONAL</span>
										{:else}
											<span class="org-type mono team">TEAM</span>
										{/if}
									</td>
									<td class="mono">{formatDate(org.created_at)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>

		{:else if activeTab === 'credits'}
			<!-- GPU Credits -->
			<div class="section">
				<h2 class="section-title mono">GRANT / REVOKE CREDITS</h2>
				<div class="grant-row">
					<select class="tier-select mono" bind:value={grantOrgId}>
						<option value="">Select org...</option>
						{#each orgs as org}
							<option value={org.org_id}>{org.name}</option>
						{/each}
					</select>
					<input type="number" class="grant-input mono" bind:value={grantHours} step="0.5" />
					<span class="grant-unit mono">hours</span>
					<button
						class="btn mono"
						class:btn-approve={grantHours > 0}
						class:btn-reject={grantHours < 0}
						onclick={grantCredits}
						disabled={granting || !grantOrgId || grantHours === 0}
					>
						{granting ? '...' : grantHours < 0 ? 'REVOKE' : 'GRANT'}
					</button>
				</div>
				<p class="grant-hint mono">Use negative hours to revoke credits.</p>
			</div>

			<div class="section">
				<h2 class="section-title mono">ALL ORG CREDITS</h2>
				<div class="table-wrap">
					<table class="data-table">
						<thead>
							<tr>
								<th class="mono">ORG</th>
								<th class="mono">CONTRIBUTED</th>
								<th class="mono">CONSUMED</th>
								<th class="mono">BALANCE</th>
							</tr>
						</thead>
						<tbody>
							{#each allCredits as c}
								<tr>
									<td>{c.org_name || c.org_id.substring(0, 12)}</td>
									<td class="mono">{c.contributed_hours}h</td>
									<td class="mono">{c.consumed_hours}h</td>
									<td class="mono" class:positive-text={c.balance_seconds >= 0} class:negative-text={c.balance_seconds < 0}>
										{c.balance_seconds >= 0 ? '+' : ''}{(c.balance_seconds / 3600).toFixed(2)}h
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>

		{:else if activeTab === 'stats'}
			<!-- Platform Stats -->
			{#if stats}
				<div class="stats-grid">
					<div class="stat-box">
						<span class="stat-value mono">{stats.users.total}</span>
						<span class="stat-label mono">USERS</span>
						<div class="stat-detail mono">
							{#each Object.entries(stats.users.by_tier) as [tier, count]}
								<span class="stat-tier">{tier}: {count}</span>
							{/each}
						</div>
					</div>
					<div class="stat-box">
						<span class="stat-value mono">{stats.orgs.total}</span>
						<span class="stat-label mono">ORGS</span>
						<span class="stat-detail mono">{stats.orgs.personal} personal, {stats.orgs.team} team</span>
					</div>
					<div class="stat-box">
						<span class="stat-value mono">{stats.gpu.total_contributed_hours}h</span>
						<span class="stat-label mono">GPU CONTRIBUTED</span>
						<span class="stat-detail mono">{stats.gpu.total_consumed_hours}h consumed</span>
					</div>
					<div class="stat-box">
						<span class="stat-value mono">{stats.nodes.online}/{stats.nodes.total}</span>
						<span class="stat-label mono">NODES ONLINE</span>
						<span class="stat-detail mono">{stats.nodes.busy} busy</span>
					</div>
					<div class="stat-box">
						<span class="stat-value mono">{stats.jobs.running}</span>
						<span class="stat-label mono">JOBS RUNNING</span>
						<span class="stat-detail mono">{stats.jobs.queued} queued, {stats.jobs.history} history</span>
					</div>
				</div>

				<!-- User Activity -->
				<div class="section">
					<h2 class="section-title mono">USER ACTIVITY</h2>
					<div class="table-wrap">
						<table class="data-table">
							<thead>
								<tr>
									<th class="mono">USER</th>
									<th class="mono">TIER</th>
									<th class="mono">ORGS</th>
									<th class="mono">ACTIONS</th>
								</tr>
							</thead>
							<tbody>
								{#each users as u (u.user_id)}
									<tr>
										<td>{displayName(u)}</td>
										<td><span class="tier-badge mono" data-tier={u.tier}>{u.tier}</span></td>
										<td class="mono org-cell">
											{#each (u.orgs ?? []) as o}
												<span class="org-chip">{o.name}</span>
											{/each}
										</td>
										<td>
											<button class="btn btn-subtle-sm mono" onclick={() => viewUserActivity(u.user_id)}>VIEW</button>
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</div>

				<!-- Activity detail modal -->
				{#if selectedUserActivity}
					<div class="activity-panel">
						<div class="activity-header">
							<h3 class="mono">{selectedUserActivity.user.name || selectedUserActivity.user.email}</h3>
							<button class="btn-icon-close" onclick={() => selectedUserActivity = null}>&times;</button>
						</div>
						<div class="activity-grid">
							<div class="activity-stat">
								<span class="activity-label mono">CONTRIBUTED</span>
								<span class="activity-value mono">{selectedUserActivity.totals.contributed_hours}h</span>
							</div>
							<div class="activity-stat">
								<span class="activity-label mono">CONSUMED</span>
								<span class="activity-value mono">{selectedUserActivity.totals.consumed_hours}h</span>
							</div>
							<div class="activity-stat">
								<span class="activity-label mono">JOBS</span>
								<span class="activity-value mono">{selectedUserActivity.jobs.total} ({selectedUserActivity.jobs.completed} done, {selectedUserActivity.jobs.failed} failed)</span>
							</div>
							<div class="activity-stat">
								<span class="activity-label mono">ORGS</span>
								<span class="activity-value mono">
									{#each selectedUserActivity.orgs as o}
										{o.name} ({o.role}){' '}
									{/each}
								</span>
							</div>
						</div>
					</div>
				{/if}
			{/if}

		{:else if activeTab === 'audit'}
			<!-- Audit Log -->
			<div class="section">
				<h2 class="section-title mono">AUDIT LOG <span class="count">{auditTotal}</span></h2>
				<div class="audit-controls">
					<select class="tier-select mono" bind:value={auditFilter} onchange={() => { auditPage = 0; loadAudit(); }}>
						<option value="">All actions</option>
						<option value="user.approved">user.approved</option>
						<option value="user.rejected">user.rejected</option>
						<option value="user.tier_changed">user.tier_changed</option>
						<option value="credits.granted">credits.granted</option>
						<option value="credits.revoked">credits.revoked</option>
						<option value="org.ip_allowlist_updated">org.ip_allowlist_updated</option>
					</select>
				</div>
				<div class="table-wrap">
					<table class="data-table">
						<thead>
							<tr>
								<th class="mono">TIME</th>
								<th class="mono">ACTION</th>
								<th class="mono">TARGET</th>
								<th class="mono">ACTOR</th>
								<th class="mono">IP</th>
							</tr>
						</thead>
						<tbody>
							{#each auditEntries as e (e.id)}
								<tr>
									<td class="mono">{e.timestamp?.substring(0, 19).replace('T', ' ') ?? '—'}</td>
									<td><span class="audit-action mono">{e.action}</span></td>
									<td class="mono">
										{e.target_type}:
										{#if e.target_type === 'user'}
											{resolveUser(e.target_id)}
										{:else}
											{(e.target_id ?? '').substring(0, 12)}
										{/if}
									</td>
									<td class="mono">{resolveUser(e.actor_user_id)}</td>
									<td class="mono">{e.ip_address ?? '—'}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				{#if auditTotal > 50}
					<div class="audit-pager">
						<button class="btn btn-subtle-sm mono" disabled={auditPage === 0} onclick={() => { auditPage--; loadAudit(); }}>PREV</button>
						<span class="mono">{auditPage * 50 + 1}–{Math.min((auditPage + 1) * 50, auditTotal)} of {auditTotal}</span>
						<button class="btn btn-subtle-sm mono" disabled={(auditPage + 1) * 50 >= auditTotal} onclick={() => { auditPage++; loadAudit(); }}>NEXT</button>
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	.admin-page {
		padding: var(--sp-6);
		max-width: 960px;
		height: 100%;
		overflow-y: auto;
	}

	.denied {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		height: 60vh;
		gap: var(--sp-3);
		color: var(--text-tertiary);
	}

	.denied-icon {
		font-family: var(--font-mono);
		font-size: 14px;
		letter-spacing: 0.2em;
		color: var(--state-error);
		padding: var(--sp-2) var(--sp-4);
		border: 1px solid var(--state-error);
		border-radius: var(--radius-sm);
	}

	.loading {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 40vh;
		color: var(--text-tertiary);
	}

	.admin-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: var(--sp-6);
		padding-bottom: var(--sp-4);
		border-bottom: 1px solid var(--border);
	}

	.page-title {
		font-size: 11px;
		letter-spacing: 0.2em;
		color: var(--text-tertiary);
		font-weight: 500;
	}

	.tab-bar {
		display: flex;
		gap: 2px;
		background: var(--surface-2);
		border-radius: var(--radius-sm);
		padding: 2px;
	}

	.tab-btn {
		font-size: 11px;
		letter-spacing: 0.08em;
		padding: 6px 14px;
		border: none;
		background: transparent;
		color: var(--text-tertiary);
		border-radius: 3px;
		cursor: pointer;
		transition: all 0.15s;
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.tab-btn:hover { color: var(--text-secondary); }
	.tab-btn.active {
		background: var(--surface-4);
		color: var(--accent);
	}

	.tab-badge {
		min-width: 16px;
		height: 14px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0 4px;
		font-size: 9px;
		font-weight: 700;
		background: var(--state-error);
		color: #000;
		border-radius: 7px;
	}

	.section {
		margin-bottom: var(--sp-6);
	}

	.section-title {
		font-size: 10px;
		letter-spacing: 0.15em;
		color: var(--text-tertiary);
		margin-bottom: var(--sp-3);
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.count {
		font-size: 9px;
		background: var(--surface-4);
		padding: 1px 6px;
		border-radius: 8px;
		color: var(--text-secondary);
	}

	/* Pending cards */
	.pending-list {
		display: flex;
		flex-direction: column;
		gap: var(--sp-2);
	}

	.pending-card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: var(--sp-3) var(--sp-4);
		background: var(--surface-1);
		border: 1px solid rgba(255, 242, 3, 0.12);
		border-radius: var(--radius-md);
	}

	.pending-info {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
	}

	.pending-email {
		font-size: 14px;
		color: var(--text-primary);
	}

	.pending-name {
		font-size: 11px;
		color: var(--text-tertiary);
	}

	.pending-profile {
		font-size: 11px;
		color: var(--accent);
	}

	.pending-usecase {
		font-size: 11px;
		color: var(--text-secondary);
		font-style: italic;
	}

	.pending-date {
		font-size: 11px;
		color: var(--text-tertiary);
	}

	.pending-actions {
		display: flex;
		gap: var(--sp-2);
	}

	.btn {
		font-size: 11px;
		letter-spacing: 0.06em;
		padding: 5px 12px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition: all 0.15s;
		background: transparent;
	}

	.btn:disabled { opacity: 0.4; cursor: not-allowed; }

	.btn-approve {
		color: var(--state-complete);
		border-color: rgba(93, 216, 121, 0.3);
	}
	.btn-approve:hover:not(:disabled) {
		background: rgba(93, 216, 121, 0.1);
	}

	.btn-reject {
		color: var(--state-error);
		border-color: rgba(255, 82, 82, 0.3);
	}
	.btn-reject:hover:not(:disabled) {
		background: rgba(255, 82, 82, 0.1);
	}

	.btn-primary {
		background: var(--accent);
		color: #000;
		font-weight: 600;
	}
	.btn-primary:hover:not(:disabled) { background: #fff; }

	.btn-copy {
		color: var(--accent);
		border-color: var(--accent-dim);
	}
	.btn-copy:hover:not(:disabled) { background: var(--accent-muted); }

	.invite-row {
		display: flex;
		flex-direction: column;
		gap: var(--sp-3);
	}

	.invite-result {
		display: flex;
		gap: var(--sp-2);
		align-items: center;
	}

	.invite-url {
		flex: 1;
		padding: 8px 12px;
		background: var(--surface-3);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--text-primary);
		font-size: 12px;
		outline: none;
	}
	.invite-url:focus { border-color: var(--accent); }

	.invite-list {
		margin-top: var(--sp-3);
	}

	.status-badge {
		font-size: 10px;
		letter-spacing: 0.06em;
		padding: 2px 8px;
		border-radius: 3px;
		font-weight: 600;
	}
	.status-badge.available {
		background: rgba(93, 216, 121, 0.12);
		color: var(--state-complete);
	}
	.status-badge.used {
		background: rgba(117, 117, 117, 0.12);
		color: var(--state-cancelled);
	}

	.org-cell { display: flex; flex-wrap: wrap; gap: 3px; }
	.org-chip {
		font-size: 10px; padding: 1px 6px; border-radius: 3px;
		background: var(--surface-4); color: var(--text-secondary);
	}
	.no-org { color: var(--text-tertiary); }

	.grant-row { display: flex; gap: var(--sp-2); align-items: center; }
	.grant-input { width: 80px; padding: 6px 10px; background: var(--surface-3); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); font-size: 13px; outline: none; text-align: center; }
	.grant-input:focus { border-color: var(--accent); }
	.grant-unit { font-size: 12px; color: var(--text-tertiary); }
	.grant-hint { font-size: 11px; color: var(--text-tertiary); margin-top: var(--sp-2); }

	.positive-text { color: var(--state-complete); }
	.negative-text { color: var(--state-error); }

	.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: var(--sp-3); margin-bottom: var(--sp-5); }
	.stat-box { display: flex; flex-direction: column; gap: var(--sp-1); padding: var(--sp-4); background: var(--surface-1); border: 1px solid var(--border); border-radius: var(--radius-md); }
	.stat-value { font-size: 24px; font-weight: 700; color: var(--accent); }
	.stat-label { font-size: 10px; letter-spacing: 0.1em; color: var(--text-tertiary); }
	.stat-detail { font-size: 11px; color: var(--text-secondary); display: flex; flex-wrap: wrap; gap: var(--sp-1); }
	.stat-tier { font-size: 10px; padding: 1px 5px; background: var(--surface-3); border-radius: 3px; }

	.btn-subtle-sm { font-size: 10px; letter-spacing: 0.06em; padding: 3px 8px; background: none; border: 1px solid var(--border); border-radius: 3px; color: var(--text-tertiary); cursor: pointer; }
	.btn-subtle-sm:hover { color: var(--accent); border-color: var(--accent); }

	.activity-panel { margin-top: var(--sp-4); padding: var(--sp-4); background: var(--surface-1); border: 1px solid var(--border); border-radius: var(--radius-md); }
	.activity-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sp-3); }
	.activity-header h3 { font-size: 13px; color: var(--accent); }
	.btn-icon-close { background: none; border: none; color: var(--text-tertiary); font-size: 18px; cursor: pointer; }
	.btn-icon-close:hover { color: var(--text-primary); }
	.activity-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
	.activity-stat { display: flex; flex-direction: column; gap: 2px; }
	.activity-label { font-size: 10px; letter-spacing: 0.08em; color: var(--text-tertiary); }
	.activity-value { font-size: 13px; color: var(--text-primary); }

	.audit-controls { margin-bottom: var(--sp-3); }
	.audit-action { font-size: 11px; color: var(--accent); }
	.audit-pager { display: flex; align-items: center; gap: var(--sp-3); margin-top: var(--sp-3); justify-content: center; }

	.form-error {
		padding: var(--sp-2) var(--sp-3);
		background: rgba(255, 82, 82, 0.08);
		border: 1px solid rgba(255, 82, 82, 0.2);
		border-radius: 6px;
		font-size: 12px;
		color: var(--state-error);
	}

	/* Data table */
	.table-wrap {
		overflow-x: auto;
	}

	.data-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 13px;
	}

	.data-table th {
		font-size: 10px;
		letter-spacing: 0.1em;
		color: var(--text-tertiary);
		text-align: left;
		padding: var(--sp-2) var(--sp-3);
		border-bottom: 1px solid var(--border);
		font-weight: 500;
	}

	.data-table td {
		padding: var(--sp-2) var(--sp-3);
		border-bottom: 1px solid var(--border-subtle);
		color: var(--text-secondary);
		vertical-align: middle;
	}

	.data-table tr:hover td {
		background: var(--surface-1);
	}

	.user-email {
		color: var(--text-primary);
		display: block;
	}

	.user-name {
		font-size: 11px;
		color: var(--text-tertiary);
	}

	/* Tier badges */
	.tier-badge {
		font-size: 10px;
		letter-spacing: 0.06em;
		padding: 2px 8px;
		border-radius: 3px;
		font-weight: 600;
	}

	.tier-badge[data-tier="pending"] {
		background: rgba(255, 242, 3, 0.12);
		color: var(--accent);
	}
	.tier-badge[data-tier="member"] {
		background: rgba(93, 216, 121, 0.12);
		color: var(--state-complete);
	}
	.tier-badge[data-tier="contributor"] {
		background: rgba(0, 154, 218, 0.12);
		color: var(--secondary);
	}
	.tier-badge[data-tier="org_admin"] {
		background: rgba(206, 147, 216, 0.12);
		color: var(--state-masked);
	}
	.tier-badge[data-tier="platform_admin"] {
		background: rgba(255, 82, 82, 0.12);
		color: var(--state-error);
	}
	.tier-badge[data-tier="rejected"] {
		background: rgba(117, 117, 117, 0.12);
		color: var(--state-cancelled);
	}

	.tier-select {
		font-size: 11px;
		padding: 4px 8px;
		background: var(--surface-3);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--text-secondary);
		cursor: pointer;
		outline: none;
	}

	.tier-select:focus {
		border-color: var(--accent);
	}

	/* Org type badges */
	.org-type {
		font-size: 10px;
		letter-spacing: 0.06em;
		padding: 2px 8px;
		border-radius: 3px;
	}

	.org-type.personal {
		background: var(--accent-muted);
		color: var(--accent-dim);
	}

	.org-type.team {
		background: var(--secondary-muted);
		color: var(--secondary);
	}
</style>
