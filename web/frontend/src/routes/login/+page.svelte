<script lang="ts">
	import { goto } from '$app/navigation';
	import { login } from '$lib/auth';

	let email = $state('');
	let password = $state('');
	let error = $state('');
	let loading = $state(false);

	async function handleLogin() {
		if (!email || !password) {
			error = 'Email and password required';
			return;
		}
		loading = true;
		error = '';
		try {
			const session = await login(email, password);
			if (session.user.tier === 'pending') {
				goto('/pending');
			} else {
				goto('/clips');
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Login failed';
		} finally {
			loading = false;
		}
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') handleLogin();
	}
</script>

<svelte:head>
	<title>Login — CorridorKey</title>
</svelte:head>

<div class="auth-page">
	<div class="auth-card">
		<img src="/Corridor_Digital_Logo.svg" alt="Corridor Digital" class="auth-logo" />
		<div class="logo-row">
			<h1 class="auth-title mono">CORRIDORKEY</h1>
			<span class="beta-badge mono">BETA</span>
		</div>
		<p class="auth-subtitle">Sign in to your account</p>

		{#if error}
			<div class="auth-error mono">{error}</div>
		{/if}

		<div class="auth-form">
			<label class="auth-field">
				<span class="field-label mono">EMAIL</span>
				<input type="email" bind:value={email} placeholder="you@studio.com" onkeydown={onKeydown} />
			</label>
			<label class="auth-field">
				<span class="field-label mono">PASSWORD</span>
				<input type="password" bind:value={password} placeholder="••••••••" onkeydown={onKeydown} />
			</label>
			<button class="auth-btn" onclick={handleLogin} disabled={loading}>
				{loading ? 'Signing in...' : 'Sign In'}
			</button>
		</div>

		<div class="auth-footer">
			<span>Have an invite? <a href="/signup">Sign up</a></span>
		</div>
	</div>
</div>

<style>
	.auth-page {
		display: flex;
		align-items: center;
		justify-content: center;
		min-height: 100vh;
		background: var(--surface-0);
		padding: var(--sp-4);
	}

	.auth-card {
		width: 100%;
		max-width: 380px;
		background: var(--surface-1);
		border: 1px solid var(--border);
		border-radius: 12px;
		padding: var(--sp-6);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--sp-4);
	}

	.auth-logo { width: 120px; filter: drop-shadow(0 0 4px rgba(255, 242, 3, 0.15)); }
	.logo-row { display: flex; align-items: center; gap: var(--sp-2); }
	.auth-title { font-size: 11px; letter-spacing: 0.2em; color: var(--text-tertiary); }
	.beta-badge {
		font-size: 9px;
		letter-spacing: 0.1em;
		padding: 2px 6px;
		border-radius: 4px;
		background: rgba(255, 242, 3, 0.12);
		color: var(--accent);
		border: 1px solid rgba(255, 242, 3, 0.2);
	}
	.auth-subtitle { font-size: 14px; color: var(--text-secondary); margin-top: calc(-1 * var(--sp-2)); }

	.auth-error {
		width: 100%;
		padding: var(--sp-2) var(--sp-3);
		background: rgba(255, 82, 82, 0.08);
		border: 1px solid rgba(255, 82, 82, 0.2);
		border-radius: 6px;
		font-size: 12px;
		color: var(--state-error);
	}

	.auth-form { width: 100%; display: flex; flex-direction: column; gap: var(--sp-3); }
	.auth-field { display: flex; flex-direction: column; gap: 4px; }
	.field-label { font-size: 10px; color: var(--text-tertiary); letter-spacing: 0.08em; }

	.auth-field input {
		padding: 10px 12px;
		background: var(--surface-3);
		border: 1px solid var(--border);
		border-radius: 6px;
		color: var(--text-primary);
		font-size: 14px;
		outline: none;
		font-family: inherit;
	}
	.auth-field input:focus { border-color: var(--accent); }
	.auth-field input::placeholder { color: var(--text-tertiary); }

	.auth-btn {
		padding: 12px;
		background: var(--accent);
		color: #000;
		border: none;
		border-radius: 6px;
		font-size: 14px;
		font-weight: 600;
		cursor: pointer;
		transition: all 0.15s;
		margin-top: var(--sp-1);
	}
	.auth-btn:hover:not(:disabled) { background: #fff; box-shadow: 0 0 16px rgba(255, 242, 3, 0.25); }
	.auth-btn:disabled { opacity: 0.5; cursor: not-allowed; }

	.auth-footer { font-size: 13px; color: var(--text-tertiary); }
	.auth-footer a { color: var(--accent); }
</style>
