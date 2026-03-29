<script lang="ts">
	import { goto } from '$app/navigation';

	let email = $state('');
	let password = $state('');
	let name = $state('');
	let company = $state('');
	let role = $state('');
	let useCase = $state('');
	let error = $state('');
	let loading = $state(false);
	let tosAccepted = $state(false);

	async function handleSignup() {
		if (!tosAccepted) {
			error = 'You must accept the Terms of Service to create an account.';
			return;
		}
		if (!email || !password) {
			error = 'Email and password required';
			return;
		}
		loading = true;
		error = '';
		try {
			const res = await fetch('/api/auth/register', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					email,
					password,
					name,
					company,
					role,
					use_case: useCase
				})
			});
			if (!res.ok) {
				const data = await res.json().catch(() => ({ detail: 'Signup failed' }));
				error = data.detail ?? 'Signup failed';
				return;
			}

			goto('/pending');
		} catch (e) {
			error = e instanceof Error ? e.message : 'Signup failed';
		} finally {
			loading = false;
		}
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') handleSignup();
	}
</script>

<svelte:head>
	<title>Sign Up — CorridorKey</title>
</svelte:head>

<div class="auth-page">
	<div class="auth-card">
		<img src="/Corridor_Digital_Logo.svg" alt="Corridor Digital" class="auth-logo" />
		<div class="logo-row">
			<h1 class="auth-title mono">CORRIDORKEY</h1>
			<span class="beta-badge mono">BETA</span>
		</div>
		<p class="auth-subtitle">Create your account</p>

		{#if error}
			<div class="auth-error mono">{error}</div>
		{/if}

		<div class="auth-form">
			<label class="auth-field">
				<span class="field-label mono">NAME</span>
				<input type="text" bind:value={name} placeholder="Your name" onkeydown={onKeydown} />
			</label>
			<label class="auth-field">
				<span class="field-label mono">EMAIL</span>
				<input type="email" bind:value={email} placeholder="you@studio.com" onkeydown={onKeydown} />
			</label>
			<label class="auth-field">
				<span class="field-label mono">PASSWORD</span>
				<input type="password" bind:value={password} placeholder="••••••••" onkeydown={onKeydown} />
			</label>

			<div class="profile-section">
				<span class="section-hint">Optional — helps us review your application faster</span>
				<label class="auth-field">
					<span class="field-label mono">COMPANY / STUDIO</span>
					<input type="text" bind:value={company} placeholder="e.g. Corridor Digital" onkeydown={onKeydown} />
				</label>
				<label class="auth-field">
					<span class="field-label mono">ROLE</span>
					<input type="text" bind:value={role} placeholder="e.g. VFX Artist, Producer, Editor" onkeydown={onKeydown} />
				</label>
				<label class="auth-field">
					<span class="field-label mono">HOW WILL YOU USE CORRIDORKEY?</span>
					<input type="text" bind:value={useCase} placeholder="e.g. Green screen compositing for short films" onkeydown={onKeydown} />
				</label>
			</div>

			<label class="tos-check">
				<input type="checkbox" bind:checked={tosAccepted} />
				<span>I agree to not redistribute content processed through this platform</span>
			</label>
			<button class="auth-btn" onclick={handleSignup} disabled={loading || !tosAccepted}>
				{loading ? 'Creating account...' : 'Create Account'}
			</button>
		</div>

		<div class="auth-footer">
			<span>Already have an account? <a href="/login">Sign in</a></span>
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

	.logo-row {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.auth-title { font-size: 11px; letter-spacing: 0.2em; color: var(--text-tertiary); }
	.auth-subtitle { font-size: 14px; color: var(--text-secondary); margin-top: calc(-1 * var(--sp-2)); }

	.beta-badge {
		font-size: 9px;
		letter-spacing: 0.1em;
		padding: 2px 6px;
		border-radius: 4px;
		background: rgba(255, 242, 3, 0.12);
		color: var(--accent);
		border: 1px solid rgba(255, 242, 3, 0.2);
	}

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

	.profile-section {
		display: flex;
		flex-direction: column;
		gap: var(--sp-3);
		padding-top: var(--sp-2);
		border-top: 1px solid var(--border);
	}

	.section-hint {
		font-size: 11px;
		color: var(--text-tertiary);
		font-style: italic;
	}

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

	.tos-check {
		display: flex;
		align-items: flex-start;
		gap: var(--sp-2);
		font-size: 12px;
		color: var(--text-secondary);
		cursor: pointer;
		line-height: 1.4;
	}

	.tos-check input {
		margin-top: 2px;
		accent-color: var(--accent);
		cursor: pointer;
	}

	.auth-footer { font-size: 13px; color: var(--text-tertiary); }
	.auth-footer a { color: var(--accent); }
</style>
