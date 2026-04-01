<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { nodes, refreshNodes, type NodeInfo } from '$lib/stores/nodes';
	import { api } from '$lib/api';
	import { toast } from '$lib/stores/toasts';
	import { getStoredUser } from '$lib/auth';

	const isAdmin = getStoredUser()?.tier === 'platform_admin';

	// Local server GPU info (admin only)
	let localGpus = $state<{ index: number; name: string; vram_total_gb: number; vram_free_gb: number }[]>([]);
	let localCpu = $state<{ cpu_percent: number; cpu_count: number; ram_total_gb: number; ram_used_gb: number; ram_free_gb: number } | null>(null);
	let localGpuEnabled = $state(true);
	let claimDelay = $state(0);

	// Inline expand state
	let expandedNode = $state<string | null>(null);
	let editingSchedule = $state<string | null>(null);
	let scheduleStart = $state('20:00');
	let scheduleEnd = $state('08:00');
	let scheduleEnabled = $state(false);
	let editingTypes = $state<string | null>(null);
	let selectedTypes = $state<Set<string>>(new Set());
	let showRepBreakdown = $state<string | null>(null);
	let viewingLogs = $state<string | null>(null);
	let logLines = $state<string[]>([]);
	let viewingHealth = $state<string | null>(null);
	let healthData = $state<{ ts: number; cpu: number; ram_used: number; ram_total: number }[]>([]);
	let healthCanvas: HTMLCanvasElement | undefined = $state();

	const ALL_JOB_TYPES = [
		{ value: 'inference', label: 'Inference' },
		{ value: 'gvm_alpha', label: 'GVM Alpha' },
		{ value: 'videomama_alpha', label: 'VideoMaMa' },
	];

	let _intervals: ReturnType<typeof setInterval>[] = [];

	onMount(() => {
		refreshNodes();
		_intervals.push(setInterval(refreshNodes, 5000));
		if (isAdmin) {
			api.system2.localGpus().then(g => localGpus = g).catch(() => {});
			api.system2.localCpu().then(c => localCpu = c).catch(() => {});
			api.system2.getLocalGpu().then(r => localGpuEnabled = r.enabled).catch(() => {});
			api.system2.getClaimDelay().then(r => claimDelay = r.seconds).catch(() => {});
			_intervals.push(setInterval(() => api.system2.localCpu().then(c => localCpu = c).catch(() => {}), 5000));
		}
	});

	onDestroy(() => _intervals.forEach(clearInterval));

	// Fleet stats
	let onlineCount = $derived($nodes.filter(n => n.status !== 'offline' && isAlive(n)).length);
	let busyCount = $derived($nodes.filter(n => n.status === 'busy').length);
	let totalGpus = $derived($nodes.reduce((s, n) => s + (n.gpus?.length || (n.gpu_name ? 1 : 0)), 0));

	function isAlive(n: NodeInfo): boolean {
		return (Date.now() / 1000 - n.last_heartbeat) < 60;
	}

	function statusClass(n: NodeInfo): string {
		if (!isAlive(n)) return 'offline';
		if (n.status === 'busy') return 'busy';
		if (n.paused) return 'paused';
		return 'online';
	}

	function statusLabel(n: NodeInfo): string {
		if (!isAlive(n)) return 'Offline';
		if (n.status === 'busy') return 'Busy';
		if (n.paused) return 'Paused';
		return 'Online';
	}

	// Node actions
	async function togglePause(node: NodeInfo) {
		try {
			if (node.paused) await api.nodes.resume(node.node_id);
			else await api.nodes.pause(node.node_id);
			refreshNodes();
		} catch (e: unknown) { toast.error(`Failed: ${e instanceof Error ? e.message : e}`); }
	}

	async function removeNode(nodeId: string) {
		try { await api.nodes.remove(nodeId); refreshNodes(); }
		catch (e: unknown) { toast.error(`Failed: ${e instanceof Error ? e.message : e}`); }
	}

	function openScheduleEditor(node: NodeInfo) {
		editingSchedule = node.node_id;
		scheduleEnabled = node.schedule.enabled;
		scheduleStart = node.schedule.start;
		scheduleEnd = node.schedule.end;
	}

	async function saveSchedule() {
		if (!editingSchedule) return;
		try {
			await api.nodes.setSchedule(editingSchedule, { enabled: scheduleEnabled, start: scheduleStart, end: scheduleEnd });
			editingSchedule = null; refreshNodes();
		} catch (e: unknown) { toast.error(`Failed: ${e instanceof Error ? e.message : e}`); }
	}

	function openTypesEditor(node: NodeInfo) {
		editingTypes = node.node_id;
		selectedTypes = new Set(node.accepted_types);
	}

	function toggleType(type: string) {
		const next = new Set(selectedTypes);
		if (next.has(type)) next.delete(type); else next.add(type);
		selectedTypes = next;
	}

	async function saveTypes() {
		if (!editingTypes) return;
		try {
			await api.nodes.setAcceptedTypes(editingTypes, [...selectedTypes]);
			editingTypes = null; refreshNodes();
		} catch (e: unknown) { toast.error(`Failed: ${e instanceof Error ? e.message : e}`); }
	}

	async function toggleHealth(nodeId: string) {
		if (viewingHealth === nodeId) { viewingHealth = null; return; }
		try {
			const res = await api.nodes.getHealth(nodeId);
			healthData = res.history;
			viewingHealth = nodeId;
			setTimeout(() => drawHealthGraph(), 0);
		} catch { healthData = []; viewingHealth = nodeId; }
	}

	function drawHealthGraph() {
		if (!healthCanvas || healthData.length < 2) return;
		const ctx = healthCanvas.getContext('2d');
		if (!ctx) return;
		const w = healthCanvas.width = healthCanvas.offsetWidth * 2;
		const h = healthCanvas.height = 80;
		ctx.clearRect(0, 0, w, h);
		const len = healthData.length;
		const xStep = w / (len - 1);
		ctx.strokeStyle = '#009ADA'; ctx.lineWidth = 2; ctx.beginPath();
		for (let i = 0; i < len; i++) {
			const x = i * xStep, y = h - (healthData[i].cpu / 100) * h;
			if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
		}
		ctx.stroke();
		ctx.strokeStyle = '#fff203'; ctx.lineWidth = 2; ctx.beginPath();
		for (let i = 0; i < len; i++) {
			const x = i * xStep;
			const ramPct = healthData[i].ram_total > 0 ? healthData[i].ram_used / healthData[i].ram_total : 0;
			const y = h - ramPct * h;
			if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
		}
		ctx.stroke();
	}

	async function toggleLogs(nodeId: string) {
		if (viewingLogs === nodeId) { viewingLogs = null; return; }
		try {
			const res = await api.nodes.getLogs(nodeId);
			logLines = res.logs; viewingLogs = nodeId;
		} catch { logLines = ['Failed to fetch logs']; viewingLogs = nodeId; }
	}

	async function toggleVisibility(node: NodeInfo) {
		const next = node.visibility === 'shared' ? 'private' : 'shared';
		try {
			const token = localStorage.getItem('ck:auth_token');
			await fetch(`/api/farm/${node.node_id}/visibility`, {
				method: 'PUT',
				headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
				body: JSON.stringify({ visibility: next }),
			});
			refreshNodes();
		} catch (e: unknown) { toast.error(`Failed: ${e instanceof Error ? e.message : e}`); }
	}

	async function toggleLocalGpu() {
		const next = !localGpuEnabled;
		try { await api.system2.setLocalGpu(next); localGpuEnabled = next; }
		catch (e: unknown) { toast.error(`Failed: ${e instanceof Error ? e.message : e}`); }
	}

	function formatTypes(node: NodeInfo): string {
		if (!node.accepted_types.length) return 'all types';
		const labels: Record<string, string> = { inference: 'Inf', gvm_alpha: 'GVM', videomama_alpha: 'VMa' };
		return node.accepted_types.map(t => labels[t] ?? t).join(', ');
	}
</script>

<svelte:head>
	<title>Nodes — CorridorKey</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<div class="header-left">
			<h1 class="page-title">Render Farm</h1>
			<div class="fleet-stats mono">
				<span class="stat"><span class="stat-dot online"></span> {onlineCount} online</span>
				<span class="stat"><span class="stat-dot busy"></span> {busyCount} busy</span>
				<span class="stat">{totalGpus} GPUs</span>
				<span class="stat">{$nodes.length} total</span>
			</div>
		</div>
		<div class="header-actions">
			<button class="btn-ghost mono" onclick={() => refreshNodes()}>Refresh</button>
			<a href="/nodes/setup" class="btn-primary mono">+ Add Node</a>
		</div>
	</header>

	<!-- Admin: local server card -->
	{#if isAdmin && localGpus.length > 0}
		<div class="local-card">
			<div class="local-header">
				<span class="local-label mono">LOCAL SERVER GPU</span>
				<label class="toggle-row">
					<input type="checkbox" checked={localGpuEnabled} onchange={toggleLocalGpu} class="toggle" />
					<span class="toggle-text mono">{localGpuEnabled ? 'ENABLED' : 'DISABLED'}</span>
				</label>
			</div>
			<div class="local-gpus">
				{#each localGpus as gpu}
					<div class="local-gpu">
						<span class="gpu-name mono">{gpu.name}</span>
						<div class="vram-bar-wrap">
							<div class="vram-bar"><div class="vram-used" style="width: {gpu.vram_total_gb > 0 ? ((gpu.vram_total_gb - gpu.vram_free_gb) / gpu.vram_total_gb) * 100 : 0}%"></div></div>
							<span class="vram-label mono">{gpu.vram_free_gb?.toFixed(1) ?? 'N/A'} / {gpu.vram_total_gb.toFixed(1)} GB</span>
						</div>
					</div>
				{/each}
			</div>
			{#if localCpu}
				<div class="local-cpu mono">
					CPU: {localCpu.cpu_percent.toFixed(0)}% · RAM: {localCpu.ram_free_gb?.toFixed(1) ?? 'N/A'} / {localCpu.ram_total_gb.toFixed(1)} GB
				</div>
			{/if}
		</div>
	{/if}

	<!-- Node grid -->
	{#if $nodes.length > 0}
		<div class="node-grid">
			{#each $nodes as node (node.node_id)}
				{@const sc = statusClass(node)}
				<div class="node-card" class:expanded={expandedNode === node.node_id}>
					<button class="card-main" onclick={() => expandedNode = expandedNode === node.node_id ? null : node.node_id}>
						<div class="card-top">
							<span class="status-dot {sc}"></span>
							<span class="node-name">{node.name}</span>
							<span class="status-label mono {sc}">{statusLabel(node)}</span>
						</div>
						{#if node.gpus?.length > 0}
							{#each node.gpus as gpu}
								<div class="gpu-row">
									<span class="gpu-name mono">{gpu.name}</span>
									<div class="vram-bar-sm"><div class="vram-used" style="width: {gpu.vram_total_gb > 0 ? ((gpu.vram_total_gb - gpu.vram_free_gb) / gpu.vram_total_gb) * 100 : 0}%"></div></div>
									<span class="vram-text mono">{gpu.vram_free_gb.toFixed(0)}G</span>
								</div>
							{/each}
						{:else if node.gpu_name}
							<div class="gpu-row">
								<span class="gpu-name mono">{node.gpu_name}</span>
								<span class="vram-text mono">{node.vram_total_gb.toFixed(0)}G</span>
							</div>
						{/if}
						<div class="card-footer">
							<span class="score mono" title="Reputation score">{node.reputation?.score ?? '—'}</span>
							{#if node.paused}<span class="tag-paused mono">PAUSED</span>{/if}
							{#if node.schedule?.enabled}<span class="tag-sched mono">{node.schedule.start}–{node.schedule.end}</span>{/if}
							{#if node.model_compiled}<span class="tag-compiled mono">COMPILED</span>{/if}
						</div>
					</button>

					{#if expandedNode === node.node_id}
						<div class="card-expand">
							<!-- Quick actions -->
							<div class="expand-actions">
								<button class="btn-sm mono" onclick={() => togglePause(node)}>{node.paused ? 'RESUME' : 'PAUSE'}</button>
								<button class="btn-sm mono" onclick={() => openScheduleEditor(node)}>SCHEDULE</button>
								<button class="btn-sm mono" onclick={() => openTypesEditor(node)}>TYPES</button>
								<button class="btn-sm mono" onclick={() => toggleHealth(node.node_id)}>HEALTH</button>
								<button class="btn-sm mono" onclick={() => toggleLogs(node.node_id)}>LOGS</button>
								{#if node.can_manage}
									<button class="btn-sm btn-danger-sm mono" onclick={() => removeNode(node.node_id)}>REMOVE</button>
								{/if}
							</div>

							<!-- Info rows -->
							<div class="expand-info">
								<div class="info-row"><span class="info-label">Node ID</span><span class="mono">{node.node_id}</span></div>
								<div class="info-row"><span class="info-label">Host</span><span class="mono">{node.host}</span></div>
								<div class="info-row"><span class="info-label">Accepted</span><span class="mono">{formatTypes(node)}</span></div>
								<div class="info-row">
								<span class="info-label">Visibility</span>
								{#if node.can_manage}
									<button class="btn-sm mono" onclick={() => toggleVisibility(node)}>{node.visibility === 'shared' ? 'SHARED → PRIVATE' : 'PRIVATE → SHARED'}</button>
								{:else}
									<span class="mono">{node.visibility}</span>
								{/if}
							</div>
								<div class="info-row"><span class="info-label">Version</span><span class="mono">{node.agent_version?.substring(0, 8) || '—'}</span></div>
								{#if node.reputation}
									<div class="info-row">
										<span class="info-label">Score breakdown</span>
										<span class="mono">
											{node.reputation.breakdown?.success?.points ?? 0} success +
											{node.reputation.breakdown?.speed?.points ?? 0} speed +
											{node.reputation.breakdown?.uptime?.points ?? 0} uptime{#if node.reputation.breakdown?.security_penalty?.points} {node.reputation.breakdown.security_penalty.points} penalty{/if}
											= {node.reputation.score}
										</span>
									</div>
								{/if}
							</div>

							<!-- Schedule editor -->
							{#if editingSchedule === node.node_id}
								<div class="editor-panel">
									<span class="editor-title mono">SCHEDULE</span>
									<label class="toggle-row"><input type="checkbox" bind:checked={scheduleEnabled} class="toggle" /> Enabled</label>
									<div class="editor-row">
										<input type="time" bind:value={scheduleStart} class="input-sm mono" />
										<span>to</span>
										<input type="time" bind:value={scheduleEnd} class="input-sm mono" />
									</div>
									<div class="editor-actions">
										<button class="btn-sm mono" onclick={saveSchedule}>SAVE</button>
										<button class="btn-sm mono" onclick={() => editingSchedule = null}>CANCEL</button>
									</div>
								</div>
							{/if}

							<!-- Types editor -->
							{#if editingTypes === node.node_id}
								<div class="editor-panel">
									<span class="editor-title mono">ACCEPTED TYPES</span>
									<div class="type-toggles">
										{#each ALL_JOB_TYPES as t}
											<label class="type-toggle">
												<input type="checkbox" checked={selectedTypes.has(t.value)} onchange={() => toggleType(t.value)} />
												<span class="mono">{t.label}</span>
											</label>
										{/each}
									</div>
									<p class="type-hint mono">Empty = accept all types</p>
									<div class="editor-actions">
										<button class="btn-sm mono" onclick={saveTypes}>SAVE</button>
										<button class="btn-sm mono" onclick={() => editingTypes = null}>CANCEL</button>
									</div>
								</div>
							{/if}

							<!-- Health graphs -->
							{#if viewingHealth === node.node_id}
								<div class="health-panel">
									<div class="health-legend mono">
										<span><span class="legend-dot" style="background: #009ADA"></span> CPU</span>
										<span><span class="legend-dot" style="background: #fff203"></span> RAM</span>
									</div>
									{#if healthData.length >= 2}
										<canvas bind:this={healthCanvas} class="health-canvas"></canvas>
									{:else}
										<span class="mono empty-hint">No health data yet</span>
									{/if}
								</div>
							{/if}

							<!-- Logs -->
							{#if viewingLogs === node.node_id}
								<pre class="log-panel mono">{logLines.join('\n')}</pre>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{:else}
		<div class="empty-state">
			<p class="empty-text">No nodes connected</p>
			<a href="/nodes/setup" class="btn-primary mono">Add your first node</a>
		</div>
	{/if}
</div>

<style>
	.page { padding: var(--sp-5) var(--sp-6); display: flex; flex-direction: column; gap: var(--sp-4); }

	.page-header { display: flex; justify-content: space-between; align-items: flex-start; }
	.header-left { display: flex; flex-direction: column; gap: var(--sp-1); }
	.page-title { font-family: var(--font-sans); font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }
	.fleet-stats { display: flex; gap: var(--sp-3); font-size: 11px; color: var(--text-tertiary); }
	.stat { display: flex; align-items: center; gap: 4px; }
	.stat-dot { width: 6px; height: 6px; border-radius: 50%; }
	.stat-dot.online { background: var(--state-complete); }
	.stat-dot.busy { background: var(--accent); }

	.header-actions { display: flex; gap: var(--sp-2); }
	.btn-ghost {
		padding: 6px 12px; font-size: 11px; color: var(--text-secondary); background: transparent;
		border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer; transition: all 0.15s;
	}
	.btn-ghost:hover { color: var(--text-primary); border-color: var(--text-tertiary); }
	.btn-primary {
		padding: 6px 14px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
		background: var(--accent); color: #000; border: none; border-radius: var(--radius-sm);
		cursor: pointer; transition: all 0.15s; text-decoration: none;
	}
	.btn-primary:hover { background: #fff; }

	/* Local server card */
	.local-card {
		background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-md);
		padding: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-2);
	}
	.local-header { display: flex; justify-content: space-between; align-items: center; }
	.local-label { font-size: 9px; letter-spacing: 0.1em; color: var(--text-tertiary); }
	.toggle-row { display: flex; align-items: center; gap: 6px; font-size: 11px; cursor: pointer; }
	.toggle { accent-color: var(--accent); }
	.toggle-text { font-size: 10px; color: var(--text-tertiary); }
	.local-gpus { display: flex; flex-direction: column; gap: var(--sp-1); }
	.local-gpu { display: flex; align-items: center; gap: var(--sp-2); }
	.local-cpu { font-size: 10px; color: var(--text-tertiary); }

	/* Node grid */
	.node-grid {
		display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: var(--sp-3);
	}

	.node-card {
		background: var(--surface-1); border: 1px solid var(--border); border-radius: var(--radius-md);
		overflow: hidden; transition: border-color 0.15s;
	}
	.node-card:hover { border-color: var(--border-active); }
	.node-card.expanded { border-color: var(--accent); grid-column: 1 / -1; }

	.card-main {
		display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-3);
		width: 100%; text-align: left; font: inherit; color: inherit; background: none; border: none;
		cursor: pointer;
	}

	.card-top { display: flex; align-items: center; gap: var(--sp-2); }
	.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
	.status-dot.online { background: var(--state-complete); box-shadow: 0 0 6px var(--state-complete); }
	.status-dot.busy { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
	.status-dot.paused { background: var(--state-queued); }
	.status-dot.offline { background: var(--text-tertiary); }
	.node-name { font-size: 14px; font-weight: 600; color: var(--text-primary); flex: 1; }
	.status-label { font-size: 9px; letter-spacing: 0.06em; }
	.status-label.online { color: var(--state-complete); }
	.status-label.busy { color: var(--accent); }
	.status-label.paused { color: var(--state-queued); }
	.status-label.offline { color: var(--text-tertiary); }

	.gpu-row { display: flex; align-items: center; gap: var(--sp-2); }
	.gpu-name { font-size: 11px; color: var(--text-secondary); flex: 1; }
	.vram-bar-wrap { display: flex; align-items: center; gap: var(--sp-2); flex: 1; }
	.vram-bar, .vram-bar-sm { height: 4px; background: var(--surface-4); border-radius: 2px; flex: 1; overflow: hidden; }
	.vram-bar-sm { max-width: 60px; }
	.vram-used { height: 100%; background: var(--accent); border-radius: 2px; transition: width 0.3s; }
	.vram-label { font-size: 10px; color: var(--text-tertiary); white-space: nowrap; }
	.vram-text { font-size: 10px; color: var(--text-tertiary); }

	.card-footer { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
	.score {
		font-size: 11px; font-weight: 600; color: var(--text-primary);
		padding: 1px 6px; background: var(--surface-3); border-radius: 3px;
	}
	.tag-paused { font-size: 8px; padding: 1px 5px; border-radius: 3px; background: rgba(144, 164, 174, 0.15); color: var(--state-queued); letter-spacing: 0.06em; }
	.tag-sched { font-size: 8px; padding: 1px 5px; border-radius: 3px; background: rgba(0, 154, 218, 0.1); color: var(--secondary); }
	.tag-compiled { font-size: 8px; padding: 1px 5px; border-radius: 3px; background: rgba(93, 216, 121, 0.1); color: var(--state-complete); letter-spacing: 0.06em; }

	/* Card expand */
	.card-expand {
		padding: var(--sp-3); border-top: 1px solid var(--border);
		display: flex; flex-direction: column; gap: var(--sp-3); background: var(--surface-2);
	}

	.expand-actions { display: flex; gap: var(--sp-1); flex-wrap: wrap; }
	.btn-sm {
		padding: 4px 10px; font-size: 10px; letter-spacing: 0.04em;
		background: var(--surface-3); border: 1px solid var(--border); border-radius: 4px;
		color: var(--text-secondary); cursor: pointer; transition: all 0.15s;
	}
	.btn-sm:hover { color: var(--text-primary); border-color: var(--text-tertiary); }
	.btn-danger-sm { color: var(--state-error); border-color: rgba(255, 82, 82, 0.3); }
	.btn-danger-sm:hover { background: rgba(255, 82, 82, 0.1); border-color: var(--state-error); }

	.expand-info { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-1); }
	.info-row { display: flex; justify-content: space-between; font-size: 11px; padding: 2px 0; }
	.info-label { color: var(--text-tertiary); }

	.editor-panel {
		background: var(--surface-3); border-radius: var(--radius-sm); padding: var(--sp-3);
		display: flex; flex-direction: column; gap: var(--sp-2);
	}
	.editor-title { font-size: 9px; letter-spacing: 0.1em; color: var(--text-tertiary); font-weight: 600; }
	.editor-row { display: flex; align-items: center; gap: var(--sp-2); font-size: 12px; color: var(--text-secondary); }
	.editor-actions { display: flex; gap: var(--sp-1); }
	.input-sm {
		padding: 4px 8px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 4px;
		color: var(--text-primary); font-size: 12px;
	}

	.type-toggles { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
	.type-toggle { display: flex; align-items: center; gap: 4px; font-size: 11px; color: var(--text-secondary); cursor: pointer; }
	.type-toggle input { accent-color: var(--accent); }
	.type-hint { font-size: 9px; color: var(--text-tertiary); }

	.health-panel { display: flex; flex-direction: column; gap: var(--sp-1); }
	.health-legend { display: flex; gap: var(--sp-3); font-size: 10px; color: var(--text-tertiary); }
	.legend-dot { display: inline-block; width: 8px; height: 3px; border-radius: 1px; margin-right: 4px; }
	.health-canvas { width: 100%; height: 40px; }
	.empty-hint { font-size: 10px; color: var(--text-tertiary); }

	.log-panel {
		font-size: 10px; color: var(--text-secondary); background: var(--surface-1);
		padding: var(--sp-2); border-radius: var(--radius-sm); max-height: 200px;
		overflow: auto; white-space: pre-wrap; word-break: break-all;
	}

	.empty-state {
		display: flex; flex-direction: column; align-items: center; justify-content: center;
		gap: var(--sp-3); padding: var(--sp-8);
	}
	.empty-text { font-size: 15px; color: var(--text-secondary); }
</style>
