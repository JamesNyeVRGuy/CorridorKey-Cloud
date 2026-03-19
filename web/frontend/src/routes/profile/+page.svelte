<script lang="ts">
	import { onMount } from 'svelte';
	import { getStoredUser, type AuthUser } from '$lib/auth';
	import { goto } from '$app/navigation';

	interface OrgInfo {
		org_id: string;
		name: string;
		owner_id: string;
		personal: boolean;
		created_at: number;
	}

	let user = $state<AuthUser | null>(null);
	let orgs = $state<OrgInfo[]>([]);
	let loading = $state(true);

	// Password change
	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let passwordError = $state('');
	let passwordSuccess = $state('');
	let changingPassword = $state(false);

	// Leave org
	let leavingOrg = $state<string | null>(null);

	async function authFetch(path: string, opts?: RequestInit) {
		const token = localStorage.getItem('ck:auth_token');
		const headers: Record<string, string> = { 'Content-Type': 'application/json' };
		if (token) headers['Authorization'] = `Bearer ${token}`;
		const res = await fetch(path, { ...opts, headers });
		if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
		return res.json();
	}

	async function loadOrgs() {
		try {
			const res = await authFetch('/api/orgs');
			orgs = res.orgs;
		} catch {
			orgs = [];
		}
	}

	async function changePassword() {
		passwordError = '';
		passwordSuccess = '';

		if (!newPassword || !confirmPassword) {
			passwordError = 'All fields are required';
			return;
		}
		if (newPassword !== confirmPassword) {
			passwordError = 'Passwords do not match';
			return;
		}
		if (newPassword.length < 6) {
			passwordError = 'Password must be at least 6 characters';
			return;
		}

		changingPassword = true;
		try {
			// Password change via server proxy
			const token = localStorage.getItem('ck:auth_token');
			const res = await fetch('/api/auth/password', {
				method: 'PUT',
				headers: {
					'Content-Type': 'application/json',
					'Authorization': `Bearer ${token}`
				},
				body: JSON.stringify({ password: newPassword })
			});
			if (!res.ok) {
				const err = await res.json().catch(() => ({}));
				throw new Error(err.error_description || err.msg || 'Failed to change password');
			}
			passwordSuccess = 'Password changed successfully';
			currentPassword = '';
			newPassword = '';
			confirmPassword = '';
		} catch (e) {
			passwordError = e instanceof Error ? e.message : 'Failed to change password';
		} finally {
			changingPassword = false;
		}
	}

	async function leaveOrg(orgId: string) {
		if (!user) return;
		leavingOrg = orgId;
		try {
			await authFetch(`/api/orgs/${encodeURIComponent(orgId)}/members/${encodeURIComponent(user.id)}`, {
				method: 'DELETE'
			});
			await loadOrgs();
		} catch {
			// silently fail
		} finally {
			leavingOrg = null;
		}
	}

	function formatDate(ts: number): string {
		if (!ts) return '—';
		return new Date(ts * 1000).toLocaleDateString('en-US', {
			month: 'short', day: 'numeric', year: 'numeric'
		});
	}

	onMount(async () => {
		user = getStoredUser();
		if (!user) {
			goto('/login');
			return;
		}
		await loadOrgs();
		loading = false;
	});
</script>

<svelte:head>
	<title>Profile — CorridorKey</title>
</svelte:head>

<div class="profile-page">
	{#if loading}
		<div class="loading mono">Loading...</div>
	{:else if user}
		<div class="profile-header">
			<h1 class="page-title mono">PROFILE</h1>
		</div>

		<!-- Account Info -->
		<div class="section">
			<h2 class="section-title mono">ACCOUNT</h2>
			<div class="info-grid">
				<div class="info-row">
					<span class="info-label mono">EMAIL</span>
					<span class="info-value">{user.email}</span>
				</div>
				<div class="info-row">
					<span class="info-label mono">USER ID</span>
					<span class="info-value mono">{user.id}</span>
				</div>
				<div class="info-row">
					<span class="info-label mono">TIER</span>
					<span class="tier-badge mono" data-tier={user.tier}>{user.tier}</span>
				</div>
			</div>
		</div>

		<!-- Change Password -->
		<div class="section">
			<h2 class="section-title mono">CHANGE PASSWORD</h2>
			<div class="password-form">
				{#if passwordError}
					<div class="form-error mono">{passwordError}</div>
				{/if}
				{#if passwordSuccess}
					<div class="form-success mono">{passwordSuccess}</div>
				{/if}
				<label class="form-field">
					<span class="field-label mono">NEW PASSWORD</span>
					<input type="password" bind:value={newPassword} placeholder="••••••••" />
				</label>
				<label class="form-field">
					<span class="field-label mono">CONFIRM PASSWORD</span>
					<input type="password" bind:value={confirmPassword} placeholder="••••••••" />
				</label>
				<button class="btn btn-primary mono" onclick={changePassword} disabled={changingPassword}>
					{changingPassword ? 'Changing...' : 'Change Password'}
				</button>
			</div>
		</div>

		<!-- Organizations -->
		<div class="section">
			<h2 class="section-title mono">ORGANIZATIONS <span class="count">{orgs.length}</span></h2>
			{#if orgs.length === 0}
				<p class="empty-text">No organization memberships.</p>
			{:else}
				<div class="org-list">
					{#each orgs as org (org.org_id)}
						<a href="/orgs" class="org-card" onclick={() => localStorage.setItem('ck:selected_org', org.org_id)}>
							<div class="org-info">
								<span class="org-name">{org.name}</span>
								{#if org.personal}
									<span class="org-type mono personal">PERSONAL</span>
								{:else}
									<span class="org-type mono team">TEAM</span>
								{/if}
							</div>
							{#if !org.personal && org.owner_id !== user.id}
								<button
									class="btn btn-subtle mono"
									onclick={() => leaveOrg(org.org_id)}
									disabled={leavingOrg === org.org_id}
								>LEAVE</button>
							{/if}
						</a>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.profile-page {
		padding: var(--sp-6);
		max-width: 640px;
		height: 100%;
		overflow-y: auto;
	}

	.loading {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 40vh;
		color: var(--text-tertiary);
	}

	.profile-header {
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

	.info-grid {
		display: flex;
		flex-direction: column;
		gap: var(--sp-2);
		background: var(--surface-1);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		padding: var(--sp-3) var(--sp-4);
	}

	.info-row {
		display: flex;
		align-items: center;
		gap: var(--sp-4);
	}

	.info-label {
		font-size: 10px;
		letter-spacing: 0.1em;
		color: var(--text-tertiary);
		min-width: 80px;
	}

	.info-value {
		font-size: 13px;
		color: var(--text-primary);
	}

	.tier-badge {
		font-size: 10px;
		letter-spacing: 0.06em;
		padding: 2px 8px;
		border-radius: 3px;
		font-weight: 600;
	}
	.tier-badge[data-tier="pending"] { background: rgba(255, 242, 3, 0.12); color: var(--accent); }
	.tier-badge[data-tier="member"] { background: rgba(93, 216, 121, 0.12); color: var(--state-complete); }
	.tier-badge[data-tier="contributor"] { background: rgba(0, 154, 218, 0.12); color: var(--secondary); }
	.tier-badge[data-tier="org_admin"] { background: rgba(206, 147, 216, 0.12); color: var(--state-masked); }
	.tier-badge[data-tier="platform_admin"] { background: rgba(255, 82, 82, 0.12); color: var(--state-error); }

	.password-form {
		display: flex;
		flex-direction: column;
		gap: var(--sp-3);
		max-width: 320px;
	}

	.form-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.field-label {
		font-size: 10px;
		color: var(--text-tertiary);
		letter-spacing: 0.08em;
	}

	.form-field input {
		padding: 10px 12px;
		background: var(--surface-3);
		border: 1px solid var(--border);
		border-radius: 6px;
		color: var(--text-primary);
		font-size: 14px;
		outline: none;
		font-family: inherit;
	}
	.form-field input:focus { border-color: var(--accent); }
	.form-field input::placeholder { color: var(--text-tertiary); }

	.form-error {
		padding: var(--sp-2) var(--sp-3);
		background: rgba(255, 82, 82, 0.08);
		border: 1px solid rgba(255, 82, 82, 0.2);
		border-radius: 6px;
		font-size: 12px;
		color: var(--state-error);
	}

	.form-success {
		padding: var(--sp-2) var(--sp-3);
		background: rgba(93, 216, 121, 0.08);
		border: 1px solid rgba(93, 216, 121, 0.2);
		border-radius: 6px;
		font-size: 12px;
		color: var(--state-complete);
	}

	.btn {
		font-size: 11px;
		letter-spacing: 0.06em;
		padding: 8px 16px;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition: all 0.15s;
	}
	.btn:disabled { opacity: 0.4; cursor: not-allowed; }

	.btn-primary {
		background: var(--accent);
		color: #000;
		font-weight: 600;
	}
	.btn-primary:hover:not(:disabled) { background: #fff; }

	.btn-subtle {
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text-tertiary);
	}
	.btn-subtle:hover:not(:disabled) {
		color: var(--state-error);
		border-color: rgba(255, 82, 82, 0.3);
	}

	.empty-text {
		font-size: 13px;
		color: var(--text-tertiary);
	}

	.org-list {
		display: flex;
		flex-direction: column;
		gap: var(--sp-2);
	}

	.org-card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: var(--sp-3) var(--sp-4);
		background: var(--surface-1);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
	}

	.org-info {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
	}

	.org-name {
		font-size: 14px;
		color: var(--text-primary);
	}

	.org-type {
		font-size: 10px;
		letter-spacing: 0.06em;
		padding: 2px 8px;
		border-radius: 3px;
	}
	.org-type.personal { background: var(--accent-muted); color: var(--accent-dim); }
	.org-type.team { background: var(--secondary-muted); color: var(--secondary); }
</style>
