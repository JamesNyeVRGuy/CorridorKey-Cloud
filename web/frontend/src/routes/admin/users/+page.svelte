<script lang="ts">
	import { onMount } from 'svelte';

	interface UserRecord {
		user_id: string;
		email: string;
		tier: string;
		name: string;
		company: string;
		role: string;
		use_case: string;
		signed_up_at: number;
		approved_at: number;
		approved_by: string;
		orgs?: { org_id: string; name: string }[];
	}

	const TIERS = ['pending', 'member', 'contributor', 'org_admin', 'platform_admin', 'rejected'];
	const PAGE_SIZES = [25, 50, 100, 200];

	let users = $state<UserRecord[]>([]);
	let loading = $state(true);
	let actionInProgress = $state<string | null>(null);
	let searchQuery = $state('');
	let tierFilter = $state('all');
	let inviteUrl = $state('');
	let inviteError = $state('');
	let inviteGenerating = $state(false);

	// Server-side pagination state
	let page = $state(1);
	let pageSize = $state(50);
	let total = $state(0);
	let pendingTotal = $state(0);
	let listLoading = $state(false);
	let searchDebounce: ReturnType<typeof setTimeout> | null = null;

	// Expandable user detail
	let expandedUser = $state<string | null>(null);
	let userActivity = $state<any>(null);
	let activityLoading = $state(false);

	// Credit grant (single user)
	let grantHours = $state('');
	let grantInProgress = $state(false);
	let grantResult = $state<{ msg: string; ok: boolean } | null>(null);

	// Bulk selection
	let selectedUsers = $state<Set<string>>(new Set());
	let bulkHours = $state('');
	let bulkInProgress = $state(false);
	let bulkResult = $state<{ msg: string; ok: boolean; failures?: { org_id: string; error: string }[] } | null>(null);

	let hasSelection = $derived(selectedUsers.size > 0);

	function toggleSelect(userId: string, e: Event) {
		e.stopPropagation();
		const next = new Set(selectedUsers);
		if (next.has(userId)) next.delete(userId); else next.add(userId);
		selectedUsers = next;
	}

	function toggleSelectAll() {
		// Operates on the currently visible page, not the full result set.
		if (selectedUsers.size === users.length && users.length > 0) {
			selectedUsers = new Set();
		} else {
			selectedUsers = new Set(users.map(u => u.user_id));
		}
	}

	function clearSelection() { selectedUsers = new Set(); bulkResult = null; }

	async function bulkGrantCredits() {
		const hours = parseFloat(bulkHours);
		if (!hours || isNaN(hours)) { bulkResult = { msg: 'Enter a valid number of hours', ok: false }; return; }

		const targets = users.filter(u => selectedUsers.has(u.user_id) && u.orgs?.length);
		if (!targets.length) { bulkResult = { msg: 'No selected users have orgs', ok: false }; return; }

		const orgIds = targets.map(u => u.orgs![0].org_id);

		bulkInProgress = true;
		bulkResult = null;
		try {
			const res = await adminFetch('/api/admin/credits/grant/bulk', {
				method: 'POST',
				body: JSON.stringify({ org_ids: orgIds, hours }),
			});
			const sign = hours > 0 ? '+' : '';
			const failures = (res.results ?? [])
				.filter((r: any) => !r.ok)
				.map((r: any) => ({ org_id: r.org_id, error: r.error || 'Unknown error' }));
			bulkResult = {
				msg: `${sign}${hours}h granted to ${res.granted} org${res.granted !== 1 ? 's' : ''}${res.failed ? `, ${res.failed} failed` : ''}`,
				ok: res.failed === 0,
				failures: failures.length > 0 ? failures : undefined,
			};
		} catch (e) {
			bulkResult = { msg: e instanceof Error ? e.message : 'Bulk grant failed', ok: false };
		}
		bulkHours = '';
		bulkInProgress = false;
	}

	async function adminFetch(path: string, opts?: RequestInit) {
		const token = localStorage.getItem('ck:auth_token');
		const headers: Record<string, string> = { 'Content-Type': 'application/json' };
		if (token) headers['Authorization'] = `Bearer ${token}`;
		const res = await fetch(path, { ...opts, headers });
		if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
		return res.json();
	}

	function buildUsersQuery(): string {
		const params = new URLSearchParams();
		params.set('limit', String(pageSize));
		params.set('offset', String((page - 1) * pageSize));
		if (tierFilter !== 'all') params.set('tier', tierFilter);
		if (searchQuery.trim()) params.set('q', searchQuery.trim());
		return params.toString();
	}

	async function loadUsers() {
		listLoading = true;
		try {
			// Main list (paginated, filtered) + a cheap count-only probe so
			// the "Pending (N)" chip stays fresh regardless of the active
			// tier filter. limit=1 gives us total without fetching the rows.
			const [allRes, pendingRes] = await Promise.all([
				adminFetch(`/api/admin/users?${buildUsersQuery()}`),
				adminFetch('/api/admin/users/pending?limit=1'),
			]);
			users = allRes.users;
			total = allRes.total ?? allRes.users.length;
			pendingTotal = pendingRes.total ?? pendingRes.users.length;
		} finally {
			listLoading = false;
		}
	}

	function filterToPending() {
		tierFilter = 'pending';
		onFiltersChanged();
	}

	function showAllTiers() {
		tierFilter = 'all';
		onFiltersChanged();
	}

	function goToPage(p: number) {
		const maxPage = Math.max(1, Math.ceil(total / pageSize));
		page = Math.min(Math.max(1, p), maxPage);
		selectedUsers = new Set();
		loadUsers().catch(() => {});
	}

	function onFiltersChanged() {
		page = 1;
		selectedUsers = new Set();
		loadUsers().catch(() => {});
	}

	function onSearchInput() {
		if (searchDebounce) clearTimeout(searchDebounce);
		searchDebounce = setTimeout(() => {
			page = 1;
			selectedUsers = new Set();
			loadUsers().catch(() => {});
		}, 300);
	}

	let totalPages = $derived(Math.max(1, Math.ceil(total / pageSize)));
	let pageStart = $derived(total === 0 ? 0 : (page - 1) * pageSize + 1);
	let pageEnd = $derived(Math.min(page * pageSize, total));

	async function approveUser(userId: string) {
		actionInProgress = userId;
		try {
			await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/approve`, { method: 'POST' });
			await loadUsers();
		} finally { actionInProgress = null; }
	}

	async function rejectUser(userId: string) {
		actionInProgress = userId;
		try {
			await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/reject`, { method: 'POST' });
			await loadUsers();
		} finally { actionInProgress = null; }
	}

	async function setTier(userId: string, tier: string) {
		actionInProgress = userId;
		try {
			await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/tier`, {
				method: 'POST', body: JSON.stringify({ tier })
			});
			await loadUsers();
		} finally { actionInProgress = null; }
	}

	async function generateInvite() {
		inviteGenerating = true; inviteUrl = ''; inviteError = '';
		try {
			const res = await adminFetch('/api/auth/invite/generate', { method: 'POST' });
			inviteUrl = `${window.location.origin}${res.signup_url}`;
		} catch (e) {
			inviteError = e instanceof Error ? e.message : 'Failed';
		} finally { inviteGenerating = false; }
	}

	async function grantCredits(orgId: string) {
		const hours = parseFloat(grantHours);
		if (!hours || isNaN(hours)) { grantResult = { msg: 'Enter a valid number of hours', ok: false }; return; }
		grantInProgress = true;
		grantResult = null;
		try {
			const res = await adminFetch('/api/admin/credits/grant', {
				method: 'POST', body: JSON.stringify({ org_id: orgId, hours })
			});
			const bal = (res.balance_seconds / 3600).toFixed(1);
			grantResult = { msg: `${hours > 0 ? '+' : ''}${hours}h applied. Balance: ${bal}h`, ok: true };
			grantHours = '';
		} catch (e) {
			grantResult = { msg: e instanceof Error ? e.message : 'Failed', ok: false };
		} finally { grantInProgress = false; }
	}

	async function toggleExpand(userId: string) {
		if (expandedUser === userId) { expandedUser = null; return; }
		expandedUser = userId;
		grantHours = ''; grantResult = null;
		activityLoading = true; userActivity = null;
		try {
			userActivity = await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/activity`);
		} catch { /* ignore */ }
		finally { activityLoading = false; }
	}

	function displayName(u: { name?: string; email: string }): string {
		return u.name?.trim() || u.email;
	}

	function formatDate(ts: number): string {
		if (!ts) return '—';
		return new Date(ts * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function timeAgo(ts: number): string {
		if (!ts) return '—';
		const seconds = Math.floor(Date.now() / 1000 - ts);
		if (seconds < 60) return 'just now';
		if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
		if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
		return `${Math.floor(seconds / 86400)}d ago`;
	}

	onMount(async () => {
		try { await loadUsers(); } catch { /* ignore */ }
		finally { loading = false; }
	});
</script>

<svelte:head>
	<title>Users — Admin — CorridorKey</title>
</svelte:head>

{#if loading}
	<div class="loading mono">Loading users...</div>
{:else}
	<!-- Pending filter chip -->
	{#if pendingTotal > 0}
		<button
			class="pending-chip mono"
			class:active={tierFilter === 'pending'}
			onclick={() => (tierFilter === 'pending' ? showAllTiers() : filterToPending())}
		>
			<span class="chip-dot"></span>
			<span>{pendingTotal} PENDING APPROVAL</span>
			<span class="chip-hint">{tierFilter === 'pending' ? 'Click to clear' : 'Click to filter'}</span>
		</button>
	{/if}

	<!-- Invite link generator -->
	<div class="invite-section">
		<div class="invite-row">
			<button class="btn-ghost mono" onclick={generateInvite} disabled={inviteGenerating}>
				{inviteGenerating ? 'Generating...' : '+ Generate Invite Link'}
			</button>
			{#if inviteUrl}
				<div class="invite-result mono">
					<input type="text" readonly value={inviteUrl} class="invite-input" />
					<button class="btn-copy mono" onclick={() => navigator.clipboard.writeText(inviteUrl)}>COPY</button>
				</div>
			{/if}
			{#if inviteError}<span class="invite-error mono">{inviteError}</span>{/if}
		</div>
	</div>

	<!-- Filter bar -->
	<div class="filter-bar">
		<input
			type="text"
			class="filter-search mono"
			placeholder="Search name, email, company..."
			bind:value={searchQuery}
			oninput={onSearchInput}
		/>
		<select class="filter-select mono" bind:value={tierFilter} onchange={onFiltersChanged}>
			<option value="all">All tiers</option>
			{#each TIERS.filter(t => t !== 'pending') as t}
				<option value={t}>{t}</option>
			{/each}
		</select>
		<label class="toggle-label mono">
			<input type="checkbox" checked={selectedUsers.size === users.length && users.length > 0} onchange={toggleSelectAll} />
			Select page
		</label>
		<span class="filter-count mono">
			{#if listLoading}loading…{:else if total === 0}0 users{:else}{pageStart}–{pageEnd} of {total}{/if}
		</span>
	</div>

	{#if hasSelection}
		<div class="bulk-bar">
			<span class="bulk-count mono">{selectedUsers.size} selected</span>
			<div class="bulk-grant">
				<input
					type="number"
					step="0.5"
					class="credit-input mono"
					placeholder="hours"
					bind:value={bulkHours}
					onkeydown={(e) => { if (e.key === 'Enter') bulkGrantCredits(); }}
				/>
				<button class="btn-grant mono" onclick={bulkGrantCredits} disabled={bulkInProgress}>
					{bulkInProgress ? '...' : 'GRANT TO ALL'}
				</button>
			</div>
			{#if bulkResult}
				<span class="grant-msg mono" class:grant-ok={bulkResult.ok} class:grant-err={!bulkResult.ok}>{bulkResult.msg}</span>
			{/if}
			<button class="btn-ghost mono bulk-clear" onclick={clearSelection}>Clear</button>
		</div>
		{#if bulkResult?.failures && bulkResult.failures.length > 0}
			<details class="bulk-failures">
				<summary class="mono">Show {bulkResult.failures.length} failure{bulkResult.failures.length !== 1 ? 's' : ''}</summary>
				<ul class="failure-list mono">
					{#each bulkResult.failures as f}
						<li><span class="failure-org">{f.org_id}</span>: {f.error}</li>
					{/each}
				</ul>
			</details>
		{/if}
	{/if}

	<!-- User list -->
	<div class="user-list">
		{#each users as u (u.user_id)}
			<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
			<div
				class="user-row"
				onclick={() => toggleExpand(u.user_id)}
				class:expanded={expandedUser === u.user_id}
				class:selected={selectedUsers.has(u.user_id)}
				class:pending={u.tier === 'pending'}
				role="button"
				tabindex="0"
				onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleExpand(u.user_id); } }}
			>
				<input type="checkbox" class="row-check" checked={selectedUsers.has(u.user_id)} onclick={(e) => toggleSelect(u.user_id, e)} />
				<span class="tier-dot" data-tier={u.tier}></span>
				<span class="user-name">{displayName(u)}</span>
				<span class="user-email mono">{u.email}</span>
				{#if u.company}<span class="user-company mono">{u.company}</span>{/if}
				<span class="tier-badge mono" data-tier={u.tier}>{u.tier}</span>
				<span class="row-actions">
					{#if u.tier === 'pending'}
						<button
							class="btn-approve-sm mono"
							title="Approve this user"
							onclick={(e) => { e.stopPropagation(); approveUser(u.user_id); }}
							disabled={actionInProgress === u.user_id}
						>APPROVE</button>
						<button
							class="btn-reject-sm mono"
							title="Reject this user"
							onclick={(e) => { e.stopPropagation(); rejectUser(u.user_id); }}
							disabled={actionInProgress === u.user_id}
						>REJECT</button>
					{/if}
				</span>
				<span class="user-date mono">{timeAgo(u.signed_up_at)}</span>
				<span class="expand-icon mono">{expandedUser === u.user_id ? '▲' : '▼'}</span>
			</div>

			{#if expandedUser === u.user_id}
				<div class="user-detail">
					<div class="detail-grid">
						<div class="detail-section">
							<h4 class="detail-title mono">PROFILE</h4>
							<div class="detail-rows">
								<div class="detail-row"><span class="detail-label">Name</span><span>{u.name || '—'}</span></div>
								<div class="detail-row"><span class="detail-label">Email</span><span class="mono">{u.email}</span></div>
								<div class="detail-row"><span class="detail-label">Company</span><span>{u.company || '—'}</span></div>
								<div class="detail-row"><span class="detail-label">Role</span><span>{u.role || '—'}</span></div>
								<div class="detail-row"><span class="detail-label">Use case</span><span>{u.use_case || '—'}</span></div>
								<div class="detail-row"><span class="detail-label">Signed up</span><span class="mono">{formatDate(u.signed_up_at)}</span></div>
								{#if u.approved_at}
									<div class="detail-row"><span class="detail-label">Approved</span><span class="mono">{formatDate(u.approved_at)}</span></div>
								{/if}
							</div>
						</div>
						<div class="detail-section">
							<h4 class="detail-title mono">ORGANIZATIONS</h4>
							{#if u.orgs && u.orgs.length > 0}
								<div class="org-tags">
									{#each u.orgs as org}
										<span class="org-tag mono">{org.name}</span>
									{/each}
								</div>
							{:else}
								<span class="detail-empty">No organizations</span>
							{/if}

							{#if activityLoading}
								<span class="detail-empty mono">Loading activity...</span>
							{:else if userActivity}
								<h4 class="detail-title mono" style="margin-top: var(--sp-3)">ACTIVITY</h4>
								<div class="detail-rows">
									<div class="detail-row"><span class="detail-label">Jobs submitted</span><span class="mono">{userActivity.job_count ?? 0}</span></div>
									<div class="detail-row"><span class="detail-label">Frames processed</span><span class="mono">{userActivity.total_frames ?? 0}</span></div>
									<div class="detail-row"><span class="detail-label">GPU time</span><span class="mono">{((userActivity.total_gpu_seconds ?? 0) / 3600).toFixed(1)}h</span></div>
								</div>
							{/if}
						</div>
					</div>
					<div class="detail-actions">
						<label class="tier-change">
							<span class="detail-label">Set tier:</span>
							<select class="filter-select mono" value={u.tier} onchange={(e) => setTier(u.user_id, (e.target as HTMLSelectElement).value)}>
								{#each TIERS as t}<option value={t}>{t}</option>{/each}
							</select>
						</label>
						{#if u.orgs && u.orgs.length > 0}
							<div class="credit-grant">
								<span class="detail-label">Credits:</span>
								<input
									type="number"
									step="0.5"
									class="credit-input mono"
									placeholder="hours"
									bind:value={grantHours}
									onkeydown={(e) => { if (e.key === 'Enter' && u.orgs?.[0]) grantCredits(u.orgs[0].org_id); }}
								/>
								<button class="btn-grant mono" onclick={() => grantCredits(u.orgs![0].org_id)} disabled={grantInProgress}>
									{grantInProgress ? '...' : 'GRANT'}
								</button>
								{#if grantResult}
									<span class="grant-msg mono" class:grant-ok={grantResult.ok} class:grant-err={!grantResult.ok}>{grantResult.msg}</span>
								{/if}
							</div>
						{/if}
					</div>
				</div>
			{/if}
		{/each}
		{#if !listLoading && users.length === 0}
			<div class="empty-row mono">No users match the current filters.</div>
		{/if}
	</div>

	<!-- Pagination controls -->
	<div class="pagination-bar">
		<label class="toggle-label mono">
			Rows
			<select class="filter-select mono" bind:value={pageSize} onchange={onFiltersChanged}>
				{#each PAGE_SIZES as n}
					<option value={n}>{n}</option>
				{/each}
			</select>
		</label>
		<div class="pagination-controls">
			<button class="btn-ghost mono" onclick={() => goToPage(1)} disabled={page <= 1 || listLoading}>« First</button>
			<button class="btn-ghost mono" onclick={() => goToPage(page - 1)} disabled={page <= 1 || listLoading}>‹ Prev</button>
			<span class="pagination-status mono">Page {page} / {totalPages}</span>
			<button class="btn-ghost mono" onclick={() => goToPage(page + 1)} disabled={page >= totalPages || listLoading}>Next ›</button>
			<button class="btn-ghost mono" onclick={() => goToPage(totalPages)} disabled={page >= totalPages || listLoading}>Last »</button>
		</div>
	</div>
{/if}

<style>
	.loading { text-align: center; padding: var(--sp-8); color: var(--text-tertiary); font-size: 12px; }

	/* Pending filter chip */
	.pending-chip {
		display: inline-flex; align-items: center; gap: var(--sp-2);
		padding: 8px 14px; font-size: 11px; letter-spacing: 0.08em; font-weight: 600;
		background: rgba(255, 242, 3, 0.08); color: var(--accent);
		border: 1px solid rgba(255, 242, 3, 0.25); border-radius: var(--radius-md);
		cursor: pointer; transition: all 0.15s; align-self: flex-start;
	}
	.pending-chip:hover { background: rgba(255, 242, 3, 0.15); }
	.pending-chip.active { background: rgba(255, 242, 3, 0.2); border-color: var(--accent); }
	.pending-chip .chip-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 6px var(--accent); }
	.pending-chip .chip-hint { font-size: 9px; color: var(--text-tertiary); font-weight: 400; letter-spacing: 0.04em; }

	/* Inline row actions (approve/reject on pending rows) */
	.row-actions { display: flex; gap: var(--sp-1); }
	.btn-approve-sm, .btn-reject-sm {
		padding: 4px 8px; font-size: 9px; letter-spacing: 0.06em; font-weight: 600;
		border-radius: var(--radius-sm); cursor: pointer; transition: all 0.15s;
	}
	.btn-approve-sm {
		background: rgba(93, 216, 121, 0.1); color: var(--state-complete);
		border: 1px solid rgba(93, 216, 121, 0.3);
	}
	.btn-approve-sm:hover:not(:disabled) { background: rgba(93, 216, 121, 0.2); }
	.btn-reject-sm {
		background: rgba(255, 82, 82, 0.1); color: var(--state-error);
		border: 1px solid rgba(255, 82, 82, 0.3);
	}
	.btn-reject-sm:hover:not(:disabled) { background: rgba(255, 82, 82, 0.2); }
	.btn-approve-sm:disabled, .btn-reject-sm:disabled { opacity: 0.4; cursor: not-allowed; }

	/* Invite section */
	.invite-section { display: flex; flex-direction: column; gap: var(--sp-2); }
	.invite-row { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
	.btn-ghost {
		padding: 6px 12px; font-size: 11px; color: var(--text-secondary); background: transparent;
		border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer; transition: all 0.15s;
	}
	.btn-ghost:hover { color: var(--text-primary); border-color: var(--accent); }
	.btn-ghost:disabled { opacity: 0.5; cursor: not-allowed; }
	.invite-result { display: flex; gap: var(--sp-1); flex: 1; min-width: 0; }
	.invite-input {
		flex: 1; padding: 6px 8px; font-size: 11px; background: var(--surface-3); border: 1px solid var(--border);
		border-radius: var(--radius-sm); color: var(--text-primary); font-family: var(--font-mono); min-width: 0;
	}
	.btn-copy {
		padding: 6px 10px; font-size: 10px; letter-spacing: 0.06em; background: var(--accent); color: #000;
		border: none; border-radius: var(--radius-sm); cursor: pointer; font-weight: 600;
	}
	.invite-error { font-size: 11px; color: var(--state-error); }

	/* Filter bar */
	.filter-bar { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; }
	.filter-search {
		flex: 1; min-width: 200px; padding: 7px 10px; background: var(--surface-2); border: 1px solid var(--border);
		border-radius: 6px; color: var(--text-primary); font-size: 12px; outline: none;
	}
	.filter-search:focus { border-color: var(--accent); }
	.filter-search::placeholder { color: var(--text-tertiary); }
	.filter-select {
		padding: 7px 10px; background: var(--surface-2); border: 1px solid var(--border);
		border-radius: 6px; color: var(--text-primary); font-size: 12px;
	}
	.toggle-label { font-size: 11px; color: var(--text-tertiary); display: flex; align-items: center; gap: 4px; cursor: pointer; }
	.toggle-label input { accent-color: var(--accent); }
	.filter-count { font-size: 10px; color: var(--text-tertiary); margin-left: auto; }

	/* Bulk action bar */
	.bulk-bar {
		display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
		padding: var(--sp-2) var(--sp-3);
		background: rgba(255, 242, 3, 0.06); border: 1px solid rgba(255, 242, 3, 0.15);
		border-radius: var(--radius-md);
	}
	.bulk-count { font-size: 11px; color: var(--accent); font-weight: 600; }
	.bulk-grant { display: flex; align-items: center; gap: var(--sp-1); }
	.bulk-clear { margin-left: auto; }
	.bulk-failures {
		margin-top: var(--sp-2); padding: var(--sp-2) var(--sp-3);
		background: rgba(255, 82, 82, 0.05); border: 1px solid rgba(255, 82, 82, 0.2);
		border-radius: var(--radius-sm); font-size: 10px; color: var(--state-error);
	}
	.bulk-failures summary { cursor: pointer; font-weight: 600; letter-spacing: 0.06em; }
	.failure-list { margin: var(--sp-2) 0 0 0; padding-left: var(--sp-4); display: flex; flex-direction: column; gap: 2px; }
	.failure-list li { color: var(--text-secondary); }
	.failure-org { color: var(--text-tertiary); }

	/* User list */
	.user-list {
		border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden;
		background: var(--surface-1);
	}

	.user-row {
		display: grid; grid-template-columns: 18px 8px 1fr 1.5fr auto auto auto auto 20px;
		gap: var(--sp-2); align-items: center; padding: var(--sp-2) var(--sp-3);
		border-bottom: 1px solid var(--border-subtle); width: 100%; text-align: left;
		font: inherit; color: inherit; background: transparent;
		cursor: pointer; transition: background 0.15s;
	}
	.user-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.user-row:hover { background: var(--surface-2); }
	.user-row:last-child { border-bottom: none; }
	.user-row.expanded { background: var(--surface-2); border-left: 3px solid var(--accent); }
	.user-row.selected { background: rgba(255, 242, 3, 0.04); }
	.user-row.pending { background: rgba(255, 242, 3, 0.05); border-left: 3px solid rgba(255, 242, 3, 0.4); }
	.user-row.pending:hover { background: rgba(255, 242, 3, 0.1); }
	.row-check { accent-color: var(--accent); cursor: pointer; margin: 0; }

	.tier-dot { width: 8px; height: 8px; border-radius: 50%; }
	.tier-dot[data-tier="pending"] { background: var(--accent); }
	.tier-dot[data-tier="member"] { background: var(--state-ready); }
	.tier-dot[data-tier="contributor"] { background: var(--state-complete); }
	.tier-dot[data-tier="org_admin"] { background: var(--state-masked); }
	.tier-dot[data-tier="platform_admin"] { background: var(--state-error); }
	.tier-dot[data-tier="rejected"] { background: var(--state-cancelled); }

	.user-name { font-size: 13px; font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.user-email { font-size: 11px; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.user-company { font-size: 10px; color: var(--secondary); white-space: nowrap; }
	.user-date { font-size: 10px; color: var(--text-tertiary); white-space: nowrap; }
	.expand-icon { font-size: 9px; color: var(--text-tertiary); }

	.tier-badge {
		font-size: 9px; padding: 2px 6px; border-radius: 3px; letter-spacing: 0.06em; white-space: nowrap;
	}
	.tier-badge[data-tier="pending"] { background: rgba(255, 242, 3, 0.12); color: var(--accent); }
	.tier-badge[data-tier="member"] { background: rgba(61, 184, 255, 0.12); color: var(--state-ready); }
	.tier-badge[data-tier="contributor"] { background: rgba(93, 216, 121, 0.12); color: var(--state-complete); }
	.tier-badge[data-tier="org_admin"] { background: rgba(206, 147, 216, 0.12); color: var(--state-masked); }
	.tier-badge[data-tier="platform_admin"] { background: rgba(255, 82, 82, 0.12); color: var(--state-error); }
	.tier-badge[data-tier="rejected"] { background: rgba(117, 117, 117, 0.12); color: var(--state-cancelled); }

	/* User detail expand */
	.user-detail {
		padding: var(--sp-4); border-bottom: 1px solid var(--border);
		background: var(--surface-2); border-left: 3px solid var(--accent);
	}
	.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-4); }
	.detail-section { display: flex; flex-direction: column; gap: var(--sp-2); }
	.detail-title { font-size: 9px; letter-spacing: 0.1em; color: var(--text-tertiary); font-weight: 600; }
	.detail-rows { display: flex; flex-direction: column; gap: var(--sp-1); }
	.detail-row { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-secondary); }
	.detail-label { color: var(--text-tertiary); }
	.detail-empty { font-size: 11px; color: var(--text-tertiary); font-style: italic; }
	.org-tags { display: flex; gap: var(--sp-1); flex-wrap: wrap; }
	.org-tag {
		font-size: 10px; padding: 2px 8px; border-radius: 3px;
		background: var(--surface-4); color: var(--text-secondary);
	}
	.detail-actions { margin-top: var(--sp-3); display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
	.tier-change { display: flex; align-items: center; gap: var(--sp-2); font-size: 12px; color: var(--text-secondary); }
	.credit-grant { display: flex; align-items: center; gap: var(--sp-2); font-size: 12px; color: var(--text-secondary); }
	.credit-input {
		width: 70px; padding: 5px 8px; background: var(--surface-3); border: 1px solid var(--border);
		border-radius: var(--radius-sm); color: var(--text-primary); font-size: 11px; text-align: right;
	}
	.credit-input:focus { border-color: var(--accent); outline: none; }
	.credit-input::placeholder { color: var(--text-tertiary); }
	.btn-grant {
		padding: 5px 10px; font-size: 10px; letter-spacing: 0.06em; font-weight: 600;
		background: rgba(93, 216, 121, 0.1); color: var(--state-complete); border: 1px solid rgba(93, 216, 121, 0.3);
		border-radius: var(--radius-sm); cursor: pointer; transition: all 0.15s;
	}
	.btn-grant:hover:not(:disabled) { background: rgba(93, 216, 121, 0.2); }
	.btn-grant:disabled { opacity: 0.4; cursor: not-allowed; }
	.grant-msg { font-size: 10px; }
	.grant-ok { color: var(--state-complete); }
	.grant-err { color: var(--state-error); }

	/* Pagination */
	.pagination-bar {
		display: flex; align-items: center; justify-content: space-between;
		gap: var(--sp-3); padding: var(--sp-2) var(--sp-1); flex-wrap: wrap;
	}
	.pagination-controls { display: flex; align-items: center; gap: var(--sp-1); }
	.pagination-status { font-size: 11px; color: var(--text-tertiary); padding: 0 var(--sp-2); }
	.empty-row { padding: var(--sp-4); text-align: center; font-size: 11px; color: var(--text-tertiary); }
</style>
