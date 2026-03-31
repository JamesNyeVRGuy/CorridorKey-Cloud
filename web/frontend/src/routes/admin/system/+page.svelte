<script lang="ts">
	import { onMount } from 'svelte';

	let bannerMessage = $state('');
	let bannerLevel = $state<'info' | 'warning' | 'critical'>('info');
	let bannerExpiry = $state('');
	let maintenanceEnabled = $state(false);
	let maintenanceStart = $state('');
	let maintenanceEnd = $state('');
	let maintenanceReason = $state('');
	let maintenanceActive = $state(false);
	let retentionEnabled = $state(true);
	let retentionDays = $state<Record<string, number>>({});
	let deleteMode = $state('outputs_only');
	let loading = $state(true);

	async function adminFetch(path: string, opts?: RequestInit) {
		const token = localStorage.getItem('ck:auth_token');
		const headers: Record<string, string> = { 'Content-Type': 'application/json' };
		if (token) headers['Authorization'] = `Bearer ${token}`;
		const res = await fetch(path, { ...opts, headers });
		if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
		return res.json();
	}

	async function loadAll() {
		const [banner, maint, retention] = await Promise.all([
			adminFetch('/api/admin/banner'),
			adminFetch('/api/admin/maintenance'),
			adminFetch('/api/admin/retention'),
		]);
		bannerMessage = banner.message || '';
		bannerLevel = banner.level || 'info';
		bannerExpiry = banner.expires_at || '';
		maintenanceEnabled = maint.enabled || false;
		maintenanceStart = maint.starts_at || '';
		maintenanceEnd = maint.ends_at || '';
		maintenanceReason = maint.reason || '';
		maintenanceActive = maint.active || false;
		retentionEnabled = retention.enabled ?? true;
		retentionDays = retention.retention_days || {};
		deleteMode = retention.delete_mode || 'outputs_only';
	}

	async function saveBanner() {
		await adminFetch('/api/admin/banner', {
			method: 'PUT', body: JSON.stringify({ message: bannerMessage, level: bannerLevel, expires_at: bannerExpiry || null }),
		});
	}
	async function clearBanner() { bannerMessage = ''; bannerExpiry = ''; await saveBanner(); }

	async function saveMaintenance() {
		const res = await adminFetch('/api/admin/maintenance', {
			method: 'PUT', body: JSON.stringify({ enabled: maintenanceEnabled, starts_at: maintenanceStart || null, ends_at: maintenanceEnd || null, reason: maintenanceReason }),
		});
		maintenanceActive = res.active || false;
	}
	async function disableMaintenance() { maintenanceEnabled = false; await saveMaintenance(); }

	async function saveRetention() {
		await adminFetch('/api/admin/retention', {
			method: 'PUT', body: JSON.stringify({ enabled: retentionEnabled, retention_days: retentionDays, delete_mode: deleteMode }),
		});
	}

	onMount(async () => {
		try { await loadAll(); } catch { /* ignore */ }
		finally { loading = false; }
	});
</script>

<svelte:head>
	<title>System — Admin — CorridorKey</title>
</svelte:head>

{#if loading}
	<div class="loading mono">Loading...</div>
{:else}
	<div class="system-grid">
		<!-- Banner -->
		<div class="system-card">
			<h3 class="card-title mono">SITE BANNER</h3>
			<div class="form">
				<label class="field">
					<span class="field-label mono">MESSAGE</span>
					<input type="text" bind:value={bannerMessage} placeholder="e.g. Scheduled maintenance tonight 10pm EST" class="input mono" />
				</label>
				<div class="field-row">
					<label class="field">
						<span class="field-label mono">LEVEL</span>
						<select bind:value={bannerLevel} class="select mono">
							<option value="info">Info (blue)</option>
							<option value="warning">Warning (yellow)</option>
							<option value="critical">Critical (red)</option>
						</select>
					</label>
					<label class="field">
						<span class="field-label mono">EXPIRES AT</span>
						<input type="datetime-local" bind:value={bannerExpiry} class="input mono" />
					</label>
				</div>
				<div class="actions">
					<button class="btn-primary mono" onclick={saveBanner}>SET BANNER</button>
					<button class="btn-secondary mono" onclick={clearBanner}>CLEAR</button>
				</div>
			</div>
		</div>

		<!-- Maintenance -->
		<div class="system-card" class:card-active={maintenanceActive}>
			<div class="card-header">
				<h3 class="card-title mono">MAINTENANCE MODE</h3>
				{#if maintenanceActive}<span class="active-badge mono">ACTIVE</span>{/if}
			</div>
			<div class="form">
				<label class="field">
					<span class="field-label mono">REASON</span>
					<input type="text" bind:value={maintenanceReason} placeholder="e.g. Server upgrade" class="input mono" />
				</label>
				<div class="field-row">
					<label class="field">
						<span class="field-label mono">STARTS AT</span>
						<input type="datetime-local" bind:value={maintenanceStart} class="input mono" />
					</label>
					<label class="field">
						<span class="field-label mono">ENDS AT</span>
						<input type="datetime-local" bind:value={maintenanceEnd} class="input mono" />
					</label>
				</div>
				<div class="actions">
					<button class="btn-primary mono" onclick={() => { maintenanceEnabled = true; saveMaintenance(); }}>ENABLE</button>
					<button class="btn-danger mono" onclick={disableMaintenance}>DISABLE</button>
				</div>
			</div>
		</div>

		<!-- Clip retention -->
		<div class="system-card wide">
			<h3 class="card-title mono">CLIP RETENTION</h3>
			<div class="form">
				<div class="field-row">
					<label class="field">
						<span class="field-label mono">STATUS</span>
						<select bind:value={retentionEnabled} class="select mono">
							<option value={true}>Enabled</option>
							<option value={false}>Disabled</option>
						</select>
					</label>
					<label class="field">
						<span class="field-label mono">DELETE MODE</span>
						<select bind:value={deleteMode} class="select mono">
							<option value="outputs_only">Outputs only (keep source)</option>
							<option value="full">Full delete</option>
						</select>
					</label>
				</div>
				<div class="retention-grid">
					{#each Object.entries(retentionDays) as [tier, days]}
						<label class="retention-row">
							<span class="tier-label mono" data-tier={tier}>{tier}</span>
							<input type="number" bind:value={retentionDays[tier]} min="-1" class="input-sm mono" />
							<span class="retention-hint mono">{days < 0 ? 'never' : `${days}d`}</span>
						</label>
					{/each}
				</div>
				<div class="actions">
					<button class="btn-primary mono" onclick={saveRetention}>SAVE</button>
				</div>
			</div>
		</div>
	</div>
{/if}

<style>
	.loading { text-align: center; padding: var(--sp-8); color: var(--text-tertiary); font-size: 12px; }

	.system-grid {
		display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3);
	}

	.system-card {
		background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-md);
		padding: var(--sp-4); display: flex; flex-direction: column; gap: var(--sp-3);
	}
	.system-card.wide { grid-column: 1 / -1; }
	.system-card.card-active { border-color: rgba(255, 82, 82, 0.4); }

	.card-header { display: flex; align-items: center; gap: var(--sp-2); }
	.card-title { font-size: 10px; letter-spacing: 0.1em; color: var(--text-tertiary); font-weight: 600; }
	.active-badge {
		font-size: 9px; padding: 2px 6px; border-radius: 4px; letter-spacing: 0.08em;
		background: rgba(255, 82, 82, 0.15); color: var(--state-error);
	}

	.form { display: flex; flex-direction: column; gap: var(--sp-3); }
	.field { display: flex; flex-direction: column; gap: 4px; flex: 1; }
	.field-label { font-size: 9px; letter-spacing: 0.08em; color: var(--text-tertiary); }
	.field-row { display: flex; gap: var(--sp-3); }

	.input {
		padding: 8px 10px; background: var(--surface-3); border: 1px solid var(--border);
		border-radius: 6px; color: var(--text-primary); font-size: 12px; outline: none; width: 100%;
	}
	.input:focus { border-color: var(--accent); }
	.input::placeholder { color: var(--text-tertiary); }
	.input-sm { width: 70px; padding: 4px 8px; background: var(--surface-3); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary); font-size: 12px; text-align: center; }

	.select {
		padding: 8px 10px; background: var(--surface-3); border: 1px solid var(--border);
		border-radius: 6px; color: var(--text-primary); font-size: 12px; width: 100%;
	}

	.actions { display: flex; gap: var(--sp-2); }

	.btn-primary {
		padding: 6px 14px; font-size: 10px; letter-spacing: 0.08em; font-weight: 600;
		background: var(--accent); color: #000; border: none; border-radius: var(--radius-sm);
		cursor: pointer; transition: all 0.15s;
	}
	.btn-primary:hover { background: #fff; }
	.btn-secondary {
		padding: 6px 14px; font-size: 10px; letter-spacing: 0.08em; font-weight: 600;
		background: transparent; color: var(--text-secondary); border: 1px solid var(--border);
		border-radius: var(--radius-sm); cursor: pointer; transition: all 0.15s;
	}
	.btn-secondary:hover { color: var(--text-primary); border-color: var(--text-tertiary); }
	.btn-danger {
		padding: 6px 14px; font-size: 10px; letter-spacing: 0.08em; font-weight: 600;
		background: rgba(255, 82, 82, 0.1); color: var(--state-error); border: 1px solid rgba(255, 82, 82, 0.3);
		border-radius: var(--radius-sm); cursor: pointer; transition: all 0.15s;
	}
	.btn-danger:hover { background: rgba(255, 82, 82, 0.2); }

	.retention-grid { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
	.retention-row { display: flex; align-items: center; gap: var(--sp-2); }
	.tier-label {
		font-size: 10px; padding: 2px 8px; border-radius: 3px; min-width: 80px; text-align: center;
	}
	.tier-label[data-tier="pending"] { background: rgba(255, 242, 3, 0.12); color: var(--accent); }
	.tier-label[data-tier="member"] { background: rgba(61, 184, 255, 0.12); color: var(--state-ready); }
	.tier-label[data-tier="contributor"] { background: rgba(93, 216, 121, 0.12); color: var(--state-complete); }
	.tier-label[data-tier="org_admin"] { background: rgba(206, 147, 216, 0.12); color: var(--state-masked); }
	.tier-label[data-tier="platform_admin"] { background: rgba(255, 82, 82, 0.12); color: var(--state-error); }
	.retention-hint { font-size: 10px; color: var(--text-tertiary); min-width: 40px; }
</style>
