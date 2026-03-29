<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { connect, disconnect, onMessage, isConnected } from '$lib/ws';
	import GuidedTour from '../components/GuidedTour.svelte';
	import { refreshClips } from '$lib/stores/clips';
	import { refreshJobs, updateJobFromWS, currentJob, runningJobs, activeJobCount } from '$lib/stores/jobs';
	import { refreshDevice, refreshVRAM, device, vram, wsConnected } from '$lib/stores/system';
	import { refreshNodes, nodes } from '$lib/stores/nodes';
	import VramMeter from '../components/VramMeter.svelte';
	import ToastContainer from '../components/ToastContainer.svelte';
	import KeyboardHelp from '../components/KeyboardHelp.svelte';
	import { toast } from '$lib/stores/toasts';
	import { goto } from '$app/navigation';
	import { logout, getStoredUser, initAuth } from '$lib/auth';
	import { userOrgs, activeOrgId, switchOrg, loadUserOrgs, type OrgSummary } from '$lib/stores/orgs';

	let { children } = $props();
	let authChecked = $state(false);
	let authEnabled = $state(false);
	let creditBalance = $state<{ hours: string; positive: boolean } | null>(null);

	let connected = $state(false);

	const baseNavItems = [
		{ href: '/clips', label: 'Clips', icon: 'film' },
		{ href: '/jobs', label: 'Jobs', icon: 'layers' },
		{ href: '/nodes', label: 'Nodes', icon: 'server' },
		{ href: '/settings', label: 'Settings', icon: 'sliders' },
	];
	const orgsNavItem = { href: '/orgs', label: 'Orgs', icon: 'org' };
	const adminNavItem = { href: '/admin', label: 'Admin', icon: 'shield' };
	let navItems = $derived.by(() => {
		const items = [...baseNavItems];
		if (authEnabled) items.push(orgsNavItem);
		if (authEnabled && getStoredUser()?.tier === 'platform_admin') items.push(adminNavItem);
		return items;
	});

	function isActive(href: string): boolean {
		return page.url.pathname === href || page.url.pathname.startsWith(href + '/');
	}


	const publicPaths = ['/login', '/signup', '/pending', '/status', '/terms', '/privacy'];
	const _hasToken = () => !!localStorage.getItem('ck:auth_token');
	// / is public only for logged-out users (landing page). Logged-in users get the sidebar.
	let isPublicPage = $derived(
		(page.url.pathname === '/' && !_hasToken()) ||
		publicPaths.some((p) => page.url.pathname.startsWith(p))
	);

	async function refreshCredits() {
		try {
			const token = localStorage.getItem('ck:auth_token');
			const headers: Record<string, string> = {};
			if (token) headers['Authorization'] = `Bearer ${token}`;
			// Use active org (from store or localStorage), not hardcoded first org
			const { getActiveOrgId: getOrg } = await import('$lib/auth');
			const orgId = getOrg();
			if (orgId) {
				const credits = await fetch(`/api/orgs/${orgId}/credits`, { headers }).then(r => r.json());
				const hrs = (credits.balance_seconds / 3600).toFixed(1);
				creditBalance = { hours: hrs, positive: credits.balance_seconds >= 0 };
			}
		} catch { /* ignore */ }
	}

	function handleLogout() {
		logout();
		window.location.href = '/login';
	}

	function handleOrgSwitch(orgId: string) {
		switchOrg(orgId);
		// Refresh all data-dependent stores for the new org context
		refreshClips();
		refreshJobs();
		if (authEnabled) refreshCredits();
	}

	onMount(async () => {
		const currentPath = page.url.pathname;
		const hasToken = !!localStorage.getItem('ck:auth_token');
		const isPublic = (currentPath === '/' && !hasToken) || publicPaths.some((p) => currentPath.startsWith(p));

		// Quick path: if we have a token and we're on app pages, show the shell
		// immediately without waiting for the auth status check. The API interceptor
		// handles 401s if the token turns out to be invalid.
		if (hasToken && !isPublic) {
			authChecked = true;
		}

		// Check auth status and persist GoTrue URL (CRKY-63)
		try {
			const { enabled } = await initAuth();
			authEnabled = enabled;

			if (enabled) {
				if (!hasToken && !isPublic) {
					// No token on an app page — redirect to login
					window.location.href = '/login';
					return;
				}
				if (hasToken && isPublic && currentPath !== '/pending' && !currentPath.startsWith('/status') && currentPath !== '/') {
					// Already logged in but on login/signup — redirect to home
					goto('/');
					return;
				}
			}
		} catch {
			// Auth endpoint not available — assume auth disabled
		}

		authChecked = true;

		// Only connect and refresh stores on app pages, not login/signup/pending
		if (isPublic) return;

		connect();
		refreshDevice();
		refreshVRAM();
		refreshClips();
		refreshJobs();
		refreshNodes();
		if (authEnabled) loadUserOrgs();

		// Load credit balance for sidebar (CRKY-6)
		if (authEnabled) refreshCredits();

		const unsubWs = onMessage((msg) => {
			if (msg.type === 'job:progress') {
				const d = msg.data as { job_id: string; clip_name: string; current: number; total: number };
				const found = updateJobFromWS(d.job_id, {
					current_frame: d.current,
					total_frames: d.total,
					status: 'running',
				});
				// Job not in stores yet — fetch it
				if (!found) refreshJobs();
			} else if (msg.type === 'job:status') {
				const d = msg.data as { job_id: string; status: string; error?: string };
				updateJobFromWS(d.job_id, { status: d.status, error_message: d.error ?? null });
				if (d.status === 'completed') {
					toast.success(`Job completed: ${d.job_id}`);

				} else if (d.status === 'failed') {
					toast.error(`Job failed: ${d.error ?? d.job_id}`);

				} else if (d.status === 'cancelled') {
					toast.warning(`Job cancelled: ${d.job_id}`);
				}
				refreshJobs();
				refreshClips();
				if (d.status === 'completed' || d.status === 'failed') refreshCredits();
			} else if (msg.type === 'job:warning') {
				const d = msg.data as { message: string };
				toast.warning(d.message);
			} else if (msg.type === 'vram:update') {
				const d = msg.data as { total: number; allocated: number; free: number; reserved: number; name: string };
				vram.set({ ...d, available: true });
			} else if (msg.type === 'clip:state_changed') {
				refreshClips();
			} else if (msg.type === 'node:update' || msg.type === 'node:offline') {
				// Re-fetch from /api/farm which applies org filtering,
				// instead of blindly pushing unfiltered WS data into the store
				refreshNodes();
			}
		});

		const connectionCheck = setInterval(() => {
			connected = isConnected();
			wsConnected.set(connected);
		}, 1000);

		const vramInterval = setInterval(refreshVRAM, 10000);

		return () => {
			unsubWs();
			clearInterval(connectionCheck);
			clearInterval(vramInterval);
			disconnect();
		};
	});
</script>

{#if isPublicPage}
	{@render children()}
{:else if authChecked}
<div class="mobile-banner">
	<span>CorridorKey is best experienced on desktop.</span>
</div>
<div class="shell">
	<nav class="sidebar">
		<div class="sidebar-top">
			<a href="/clips" class="logo">
				<img src="/Corridor_Digital_Logo.svg" alt="Corridor Digital" class="logo-img" />
				<span class="logo-product mono">CORRIDORKEY</span>
				<span class="beta-badge mono">BETA</span>
			</a>

			{#if authEnabled && $userOrgs.length > 1}
				<div class="org-switcher">
					<select
						class="org-select mono"
						value={$activeOrgId}
						onchange={(e) => handleOrgSwitch((e.target as HTMLSelectElement).value)}
						aria-label="Switch workspace"
					>
						{#each $userOrgs as org (org.org_id)}
							<option value={org.org_id}>{org.name}</option>
						{/each}
					</select>
				</div>
			{:else if authEnabled && $userOrgs.length === 1}
				<div class="org-label mono">{$userOrgs[0]?.name}</div>
			{/if}

			<div class="nav-links">
				{#each navItems as item}
					<a
						href={item.href}
						class="nav-link"
						class:active={isActive(item.href)}
					>
						<span class="nav-icon">
							{#if item.icon === 'film'}
								<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="2" y="3" width="12" height="10" rx="1.5" stroke="currentColor" stroke-width="1.2"/><path d="M5 3v10M11 3v10M2 6.5h3M11 6.5h3M2 9.5h3M11 9.5h3" stroke="currentColor" stroke-width="1.0"/></svg>
							{:else if item.icon === 'layers'}
								<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 8l6 3.5L14 8M2 10.5l6 3.5 6-3.5M2 5.5L8 9l6-3.5L8 2 2 5.5z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>
							{:else if item.icon === 'server'}
								<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="2" y="2" width="12" height="4" rx="1" stroke="currentColor" stroke-width="1.2"/><rect x="2" y="10" width="12" height="4" rx="1" stroke="currentColor" stroke-width="1.2"/><path d="M2 8h12" stroke="currentColor" stroke-width="1.0" stroke-dasharray="2 1.5"/><circle cx="5" cy="4" r="0.8" fill="currentColor"/><circle cx="5" cy="12" r="0.8" fill="currentColor"/></svg>
							{:else if item.icon === 'sliders'}
								<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><line x1="2" y1="4" x2="14" y2="4" stroke="currentColor" stroke-width="1.2"/><line x1="2" y1="8" x2="14" y2="8" stroke="currentColor" stroke-width="1.2"/><line x1="2" y1="12" x2="14" y2="12" stroke="currentColor" stroke-width="1.2"/><circle cx="5" cy="4" r="1.5" fill="var(--surface-2)" stroke="currentColor" stroke-width="1.2"/><circle cx="10" cy="8" r="1.5" fill="var(--surface-2)" stroke="currentColor" stroke-width="1.2"/><circle cx="7" cy="12" r="1.5" fill="var(--surface-2)" stroke="currentColor" stroke-width="1.2"/></svg>
							{:else if item.icon === 'org'}
								<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="5" r="2.5" stroke="currentColor" stroke-width="1.2"/><path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5" stroke="currentColor" stroke-width="1.2"/></svg>
							{:else if item.icon === 'shield'}
								<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 1.5L3 3.5v4c0 3.5 2.5 5.5 5 6.5 2.5-1 5-3 5-6.5v-4L8 1.5z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>
							{/if}
						</span>
						<span class="nav-label">{item.label}</span>
						{#if item.href === '/jobs' && $activeJobCount > 0}
							<span class="nav-badge mono">{$activeJobCount}</span>
						{/if}
						{#if item.href === '/nodes' && $nodes.length > 0}
							<span class="nav-badge mono">{$nodes.length}</span>
						{/if}
					</a>
				{/each}
			</div>
		</div>

		<div class="sidebar-bottom">
			{#if !authEnabled || getStoredUser()?.tier === 'platform_admin'}
				<VramMeter />
				<div class="device-row">
					<span class="device-dot" class:online={connected}></span>
					<span class="device-label mono">{$device}</span>
					<span class="conn-badge mono" class:live={connected}>{connected ? 'LIVE' : 'OFFLINE'}</span>
				</div>
			{:else}
				{#if creditBalance}
					<div class="credit-indicator" class:positive={creditBalance.positive} class:negative={!creditBalance.positive}>
						<span class="credit-icon mono">GPU</span>
						<span class="credit-amt mono">{creditBalance.positive ? '+' : ''}{creditBalance.hours}h</span>
					</div>
				{/if}
				<div class="device-row">
					<span class="device-dot" class:online={connected}></span>
					<span class="conn-badge mono" class:live={connected}>{connected ? 'CONNECTED' : 'OFFLINE'}</span>
				</div>
			{/if}
			{#if authEnabled}
				{@const user = getStoredUser()}
				<div class="user-row">
					<a href="/profile" class="user-email mono">{user?.name || user?.email || ''}</a>
					<button class="logout-btn mono" onclick={handleLogout}>LOGOUT</button>
				</div>
			{/if}
		</div>
	</nav>

	<main class="content">
		{#each $runningJobs as rJob (rJob.id)}
			<div class="activity-bar">
				<div class="activity-info mono">
					<span class="activity-type">{rJob.job_type.replace('_', ' ')}</span>
					<span class="activity-clip">{rJob.clip_name}</span>
					{#if rJob.total_frames > 0 && rJob.current_frame >= rJob.total_frames}
						<span class="activity-pct uploading">Uploading...</span>
					{:else if rJob.total_frames > 0}
						<span class="activity-pct">{Math.round((rJob.current_frame / rJob.total_frames) * 100)}%</span>
					{/if}
				</div>
				<div class="activity-track">
					<div
						class="activity-fill"
						style="width: {rJob.total_frames > 0 ? (rJob.current_frame / rJob.total_frames) * 100 : 0}%"
					></div>
				</div>
			</div>
		{/each}
		{@render children()}
	</main>
</div>
{/if}

{#if authChecked && !isPublicPage}
	<GuidedTour />
{/if}
<ToastContainer />
<KeyboardHelp />

<style>
	.mobile-banner {
		display: none;
		background: var(--surface-2);
		color: var(--text-secondary);
		text-align: center;
		padding: 8px 16px;
		font-size: 12px;
		border-bottom: 1px solid var(--border);
	}
	@media (max-width: 768px) {
		.mobile-banner { display: block; }
	}
	.shell {
		display: flex;
		height: 100vh;
		overflow: hidden;
	}

	.sidebar {
		width: var(--sidebar-w);
		min-width: var(--sidebar-w);
		background: var(--surface-1);
		border-right: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		justify-content: space-between;
		overflow: hidden;
		position: relative;
	}

	.sidebar::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 1px;
		background: linear-gradient(90deg, transparent, var(--accent), transparent);
		opacity: 0.5;
	}

	.sidebar-top {
		display: flex;
		flex-direction: column;
	}

	.logo {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--sp-2);
		padding: var(--sp-4);
		border-bottom: 1px solid var(--border);
		transition: background 0.2s;
	}

	.logo:hover {
		background: var(--surface-2);
	}

	.logo-img {
		width: 150px;
		height: auto;
		filter: drop-shadow(0 0 4px rgba(255, 242, 3, 0.15));
	}

	.org-switcher {
		padding: var(--sp-2) var(--sp-4);
		border-bottom: 1px solid var(--border);
	}

	.org-select {
		width: 100%;
		background: var(--surface-3);
		color: var(--text-primary);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 5px 8px;
		font-size: 11px;
		cursor: pointer;
		appearance: auto;
	}

	.org-select:focus-visible {
		outline: 1.5px solid var(--accent);
		outline-offset: 1px;
	}

	.org-label {
		padding: 6px var(--sp-4);
		font-size: 11px;
		color: var(--text-secondary);
		border-bottom: 1px solid var(--border);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.logo-product {
		font-size: 9px;
		letter-spacing: 0.2em;
		color: var(--text-tertiary);
		font-weight: 500;
	}

	.beta-badge {
		font-size: 8px;
		letter-spacing: 0.1em;
		padding: 1px 5px;
		border-radius: 3px;
		background: rgba(255, 242, 3, 0.12);
		color: var(--accent);
		border: 1px solid rgba(255, 242, 3, 0.2);
		margin-left: 2px;
	}

	.nav-links {
		display: flex;
		flex-direction: column;
		padding: var(--sp-3) var(--sp-2);
		gap: 1px;
	}

	.nav-link {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
		padding: 8px var(--sp-3);
		border-radius: var(--radius-md);
		color: var(--text-secondary);
		font-size: 14px;
		font-weight: 500;
		transition: all 0.15s ease;
		position: relative;
	}

	.nav-link:hover {
		color: var(--text-primary);
		background: var(--surface-3);
	}

	.nav-link.active {
		color: var(--accent);
		background: var(--accent-muted);
	}

	.nav-link.active::before {
		content: '';
		position: absolute;
		left: 0;
		top: 50%;
		transform: translateY(-50%);
		width: 3px;
		height: 16px;
		background: var(--accent);
		border-radius: 0 3px 3px 0;
		box-shadow: 0 0 8px rgba(255, 242, 3, 0.3);
	}

	.nav-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		flex-shrink: 0;
	}

	.sidebar-bottom {
		padding: var(--sp-3) var(--sp-4) var(--sp-4);
		border-top: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		gap: var(--sp-3);
	}

	.device-row {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.device-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--state-error);
		flex-shrink: 0;
		transition: all 0.3s ease;
	}

	.device-dot.online {
		background: var(--accent);
		box-shadow: 0 0 6px rgba(255, 242, 3, 0.4);
	}

	.device-label {
		flex: 1;
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.conn-badge {
		font-size: 9px;
		letter-spacing: 0.08em;
		color: var(--state-error);
		padding: 1px 5px;
		border: 1px solid currentColor;
		border-radius: 3px;
		opacity: 0.7;
	}

	.conn-badge.live {
		color: var(--accent);
		opacity: 1;
	}

	.nav-badge {
		margin-left: auto;
		min-width: 18px;
		height: 16px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0 4px;
		font-size: 9px;
		font-weight: 700;
		background: var(--accent);
		color: #000;
		border-radius: 8px;
	}

	.content {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
		background: var(--surface-0);
		display: flex;
		flex-direction: column;
	}

	.activity-bar {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: var(--sp-3);
		padding: 5px var(--sp-6);
		background: var(--surface-1);
		border-bottom: 1px solid var(--border);
	}

	.activity-info {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		font-size: 11px;
		white-space: nowrap;
		flex-shrink: 0;
	}

	.activity-type {
		color: var(--accent);
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.activity-clip {
		color: var(--text-secondary);
	}

	.activity-pct {
		color: var(--text-primary);
		font-weight: 600;
	}
	.activity-pct.uploading {
		color: var(--secondary);
		animation: pulse-upload 1.5s ease-in-out infinite;
	}
	@keyframes pulse-upload {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.5; }
	}

	.activity-track {
		flex: 1;
		height: 3px;
		background: var(--surface-3);
		border-radius: 2px;
		overflow: hidden;
	}

	.activity-fill {
		height: 100%;
		background: var(--accent);
		border-radius: 2px;
		transition: width 0.3s ease-out;
		box-shadow: 0 0 6px rgba(255, 242, 3, 0.2);
	}

	.credit-indicator {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		padding: var(--sp-1) var(--sp-2);
		border-radius: var(--radius-sm);
	}
	.credit-indicator.positive { background: rgba(93, 216, 121, 0.08); }
	.credit-indicator.negative { background: rgba(255, 82, 82, 0.08); }
	.credit-icon {
		font-size: 9px;
		letter-spacing: 0.06em;
		color: var(--text-tertiary);
	}
	.credit-amt {
		font-size: 12px;
		font-weight: 600;
	}
	.credit-indicator.positive .credit-amt { color: var(--state-complete); }
	.credit-indicator.negative .credit-amt { color: var(--state-error); }

	.user-row {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		padding-top: var(--sp-2);
		border-top: 1px solid var(--border);
	}

	.user-email {
		flex: 1;
		font-size: 10px;
		color: var(--text-tertiary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.logout-btn {
		font-size: 9px;
		letter-spacing: 0.08em;
		color: var(--text-tertiary);
		background: none;
		border: 1px solid var(--border);
		border-radius: 3px;
		padding: 2px 6px;
		cursor: pointer;
		transition: all 0.15s;
		flex-shrink: 0;
	}
	.logout-btn:hover {
		color: var(--state-error);
		border-color: var(--state-error);
	}
</style>
