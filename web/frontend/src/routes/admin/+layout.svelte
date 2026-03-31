<script lang="ts">
	import { page } from '$app/state';
	import { getStoredUser } from '$lib/auth';
	import { onMount } from 'svelte';

	let { children } = $props();
	let authorized = $state(false);
	let checked = $state(false);

	onMount(() => {
		const user = getStoredUser();
		authorized = user?.tier === 'platform_admin';
		checked = true;
	});

	const navItems = [
		{ href: '/admin', label: 'Overview', icon: 'grid' },
		{ href: '/admin/users', label: 'Users', icon: 'users' },
		{ href: '/admin/system', label: 'System', icon: 'settings' },
		{ href: '/admin/audit', label: 'Audit Log', icon: 'scroll' },
	];

	function isActive(href: string): boolean {
		if (href === '/admin') return page.url.pathname === '/admin';
		return page.url.pathname.startsWith(href);
	}
</script>

{#if checked && !authorized}
	<div class="denied-wrap">
		<div class="denied-card">
			<span class="denied-label mono">ACCESS DENIED</span>
			<p class="denied-text">Platform admin access required.</p>
			<a href="/clips" class="denied-link mono">Back to dashboard</a>
		</div>
	</div>
{:else if checked}
	<div class="admin-shell">
		<header class="admin-header">
			<h1 class="admin-title">Admin</h1>
			<nav class="admin-nav">
				{#each navItems as item}
					<a
						href={item.href}
						class="nav-item mono"
						class:active={isActive(item.href)}
					>{item.label}</a>
				{/each}
			</nav>
		</header>
		<div class="admin-content">
			{@render children()}
		</div>
	</div>
{/if}

<style>
	.denied-wrap {
		display: flex; align-items: center; justify-content: center;
		min-height: 60vh; padding: var(--sp-6);
	}
	.denied-card {
		text-align: center; display: flex; flex-direction: column;
		align-items: center; gap: var(--sp-3);
	}
	.denied-label {
		font-size: 11px; letter-spacing: 0.15em; color: var(--state-error);
		padding: var(--sp-2) var(--sp-4); border: 1px solid rgba(255, 82, 82, 0.3);
		border-radius: var(--radius-sm);
	}
	.denied-text { font-size: 14px; color: var(--text-secondary); }
	.denied-link { font-size: 12px; color: var(--accent); }

	.admin-shell {
		padding: var(--sp-5) var(--sp-6);
		display: flex; flex-direction: column; gap: var(--sp-4);
		max-width: 1200px;
	}

	.admin-header {
		display: flex; flex-direction: column; gap: var(--sp-3);
	}

	.admin-title {
		font-family: var(--font-sans); font-size: 22px; font-weight: 700;
		letter-spacing: -0.02em;
	}

	.admin-nav {
		display: flex; gap: 0; border-bottom: 1px solid var(--border);
	}

	.nav-item {
		padding: var(--sp-2) var(--sp-4); font-size: 11px; font-weight: 600;
		letter-spacing: 0.06em; color: var(--text-tertiary); text-decoration: none;
		border-bottom: 2px solid transparent; transition: all 0.15s;
	}
	.nav-item:hover { color: var(--text-secondary); }
	.nav-item.active { color: var(--accent); border-bottom-color: var(--accent); }

	.admin-content { display: flex; flex-direction: column; gap: var(--sp-4); }
</style>
