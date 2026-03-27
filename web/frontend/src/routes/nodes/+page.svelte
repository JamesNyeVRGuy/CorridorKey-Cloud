<script lang="ts">
	import { onMount } from 'svelte';
	import { nodes, refreshNodes, type NodeInfo } from '$lib/stores/nodes';
	import { api } from '$lib/api';
	import { toast } from '$lib/stores/toasts';
	import { getStoredUser } from '$lib/auth';

	const isAdmin = getStoredUser()?.tier === 'platform_admin';

	interface LocalGPU {
		index: number;
		name: string;
		vram_total_gb: number;
		vram_free_gb: number;
	}

	let localGpus = $state<LocalGPU[]>([]);
	let localCpu = $state<{ cpu_percent: number; cpu_count: number; ram_total_gb: number; ram_used_gb: number; ram_free_gb: number } | null>(null);
	let localGpuEnabled = $state(true);
	let claimDelay = $state(0);
	let editingSchedule = $state<string | null>(null);
	let scheduleStart = $state('20:00');
	let scheduleEnd = $state('08:00');
	let scheduleEnabled = $state(false);
	let editingTypes = $state<string | null>(null);
	let selectedTypes = $state<Set<string>>(new Set());

	// Setup guide state
	let showSetupGuide = $state(false);
	let setupInfo = $state<{ main_url: string; image: string } | null>(null);
	let gpuVendor = $state<'nvidia' | 'amd'>('nvidia');
	let nodeImage = $derived(setupInfo ? setupInfo.image.replace(/:[\w.-]+$/, `:${gpuVendor}`) : '');
	let generatedToken = $state('');
	let generatedTokenLabel = $state('');
	let tokenLabel = $state('');
	let tokenGenerating = $state(false);
	let nodeTokens = $state<{ token_preview: string; label: string; org_id: string; node_id: string | null; revoked: boolean; created_at: number }[]>([]);
	let showRevokedTokens = $state(false);
	let showRepBreakdown = $state<string | null>(null);
	let userOrgs = $state<{ org_id: string; name: string }[]>([]);
	let selectedOrgId = $state('');

	// Extract and stitch always run locally (CPU-bound, need source files on disk)
	const ALL_JOB_TYPES = [
		{ value: 'inference', label: 'Inference', kind: 'gpu' },
		{ value: 'gvm_alpha', label: 'GVM Alpha', kind: 'gpu' },
		{ value: 'videomama_alpha', label: 'VideoMaMa', kind: 'gpu' },
	];
	let viewingLogs = $state<string | null>(null);
	let logLines = $state<string[]>([]);

	onMount(() => {
		refreshNodes();
		if (isAdmin) {
			api.system2.localGpus().then((gpus) => (localGpus = gpus)).catch(() => {});
			api.system2.localCpu().then((c) => (localCpu = c)).catch(() => {});
			api.system2.getLocalGpu().then((r) => (localGpuEnabled = r.enabled)).catch(() => {});
			api.system2.getClaimDelay().then((r) => (claimDelay = r.seconds)).catch(() => {});
		}
		const interval = setInterval(refreshNodes, 5000);
		const cpuInterval = isAdmin ? setInterval(() => {
			api.system2.localCpu().then((c) => (localCpu = c)).catch(() => {});
		}, 5000) : null;
		return () => { clearInterval(interval); if (cpuInterval) clearInterval(cpuInterval); };
	});

	async function openSetupGuide() {
		if (showSetupGuide) {
			showSetupGuide = false;
			return;
		}
		showSetupGuide = true;
		try {
			const [setup, orgsRes, tokensRes] = await Promise.all([
				api.nodes.list().then(() => null).catch(() => null),  // just to warm up
				fetch('/api/orgs', { headers: { 'Authorization': `Bearer ${localStorage.getItem('ck:auth_token')}` } }).then(r => r.json()),
				fetch('/api/farm/tokens', { headers: { 'Authorization': `Bearer ${localStorage.getItem('ck:auth_token')}` } }).then(r => r.json()),
			]);
			setupInfo = await fetch('/api/farm/setup', { headers: { 'Authorization': `Bearer ${localStorage.getItem('ck:auth_token')}` } }).then(r => r.json());
			userOrgs = orgsRes?.orgs ?? [];
			nodeTokens = tokensRes?.tokens ?? [];
			if (userOrgs.length > 0 && !selectedOrgId) selectedOrgId = userOrgs[0].org_id;
		} catch { /* ignore */ }
	}

	async function generateNodeToken() {
		if (!tokenLabel.trim() || !selectedOrgId) return;
		tokenGenerating = true;
		generatedToken = '';
		try {
			const res = await fetch('/api/farm/tokens', {
				method: 'POST',
				headers: {
					'Authorization': `Bearer ${localStorage.getItem('ck:auth_token')}`,
					'Content-Type': 'application/json'
				},
				body: JSON.stringify({ org_id: selectedOrgId, label: tokenLabel.trim() })
			});
			const data = await res.json();
			if (res.ok) {
				generatedToken = data.token;
				generatedTokenLabel = tokenLabel.trim();
				tokenLabel = '';
				// Refresh token list
				const tokensRes = await fetch('/api/farm/tokens', { headers: { 'Authorization': `Bearer ${localStorage.getItem('ck:auth_token')}` } }).then(r => r.json());
				nodeTokens = tokensRes?.tokens ?? [];
			}
		} catch { /* ignore */ }
		finally { tokenGenerating = false; }
	}

	async function revokeToken(preview: string) {
		// Strip the trailing "..." from token_preview to get the 8-char prefix
		const prefix = preview.replace(/\.+$/, '');
		await fetch(`/api/farm/tokens/${encodeURIComponent(prefix)}`, {
			method: 'DELETE',
			headers: { 'Authorization': `Bearer ${localStorage.getItem('ck:auth_token')}` }
		});
		nodeTokens = nodeTokens.filter(t => t.token_preview !== preview);
	}

	function timeSince(ts: number): string {
		const seconds = Math.floor(Date.now() / 1000 - ts);
		if (seconds < 10) return 'just now';
		if (seconds < 60) return `${seconds}s ago`;
		if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
		return `${Math.floor(seconds / 3600)}h ago`;
	}

	function statusClass(status: string): string {
		if (status === 'online') return 'status-online';
		if (status === 'busy') return 'status-busy';
		return 'status-offline';
	}

	async function toggleLocalGpu() {
		const next = !localGpuEnabled;
		try {
			await api.system2.setLocalGpu(next);
			localGpuEnabled = next;
		} catch (e: unknown) {
			toast.error(`Failed: ${e instanceof Error ? e.message : e}`);
		}
	}

	async function removeNode(nodeId: string) {
		try {
			await api.nodes.remove(nodeId);
			refreshNodes();
		} catch (e: unknown) {
			toast.error(`Failed to remove node: ${e instanceof Error ? e.message : e}`);
		}
	}

	async function togglePause(node: NodeInfo) {
		try {
			if (node.paused) {
				await api.nodes.resume(node.node_id);
			} else {
				await api.nodes.pause(node.node_id);
			}
			refreshNodes();
		} catch (e: unknown) {
			toast.error(`Failed: ${e instanceof Error ? e.message : e}`);
		}
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
			await api.nodes.setSchedule(editingSchedule, {
				enabled: scheduleEnabled,
				start: scheduleStart,
				end: scheduleEnd
			});
			editingSchedule = null;
			refreshNodes();
		} catch (e: unknown) {
			toast.error(`Failed: ${e instanceof Error ? e.message : e}`);
		}
	}

	function cancelScheduleEdit() {
		editingSchedule = null;
	}

	function openTypesEditor(node: NodeInfo) {
		editingTypes = node.node_id;
		selectedTypes = new Set(node.accepted_types);
	}

	function toggleType(type: string) {
		const next = new Set(selectedTypes);
		if (next.has(type)) next.delete(type);
		else next.add(type);
		selectedTypes = next;
	}

	async function saveTypes() {
		if (!editingTypes) return;
		try {
			await api.nodes.setAcceptedTypes(editingTypes, [...selectedTypes]);
			editingTypes = null;
			refreshNodes();
		} catch (e: unknown) {
			toast.error(`Failed: ${e instanceof Error ? e.message : e}`);
		}
	}

	function cancelTypesEdit() {
		editingTypes = null;
	}

	let viewingHealth = $state<string | null>(null);
	let healthData = $state<{ ts: number; cpu: number; ram_used: number; ram_total: number }[]>([]);
	let healthCanvas: HTMLCanvasElement | undefined = $state();

	async function toggleHealth(nodeId: string) {
		if (viewingHealth === nodeId) {
			viewingHealth = null;
			return;
		}
		try {
			const res = await api.nodes.getHealth(nodeId);
			healthData = res.history;
			viewingHealth = nodeId;
			// Draw after DOM updates
			setTimeout(() => drawHealthGraph(), 0);
		} catch {
			healthData = [];
			viewingHealth = nodeId;
		}
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

		// CPU line (blue)
		ctx.strokeStyle = '#009ADA';
		ctx.lineWidth = 2;
		ctx.beginPath();
		for (let i = 0; i < len; i++) {
			const x = i * xStep;
			const y = h - (healthData[i].cpu / 100) * h;
			if (i === 0) ctx.moveTo(x, y);
			else ctx.lineTo(x, y);
		}
		ctx.stroke();

		// RAM line (yellow)
		ctx.strokeStyle = '#fff203';
		ctx.lineWidth = 2;
		ctx.beginPath();
		for (let i = 0; i < len; i++) {
			const x = i * xStep;
			const ramPct = healthData[i].ram_total > 0 ? healthData[i].ram_used / healthData[i].ram_total : 0;
			const y = h - ramPct * h;
			if (i === 0) ctx.moveTo(x, y);
			else ctx.lineTo(x, y);
		}
		ctx.stroke();
	}

	async function toggleLogs(nodeId: string) {
		if (viewingLogs === nodeId) {
			viewingLogs = null;
			return;
		}
		try {
			const res = await api.nodes.getLogs(nodeId);
			logLines = res.logs;
			viewingLogs = nodeId;
		} catch {
			logLines = ['Failed to fetch logs'];
			viewingLogs = nodeId;
		}
	}

	function formatTypes(node: NodeInfo): string {
		if (!node.accepted_types.length) return 'all';
		const labels: Record<string, string> = {
			inference: 'Inf',
			gvm_alpha: 'GVM',
			videomama_alpha: 'VMa',
			video_extract: 'Ext',
			video_stitch: 'Stitch'
		};
		return node.accepted_types.map((t) => labels[t] ?? t).join(', ');
	}

	function formatSchedule(node: NodeInfo): string {
		if (!node.schedule.enabled) return '';
		return `${node.schedule.start} — ${node.schedule.end}`;
	}
</script>

<svelte:head>
	<title>Nodes — CorridorKey</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<h1 class="mono">RENDER FARM</h1>
		<p class="subtitle">GPU processing, remote nodes, and scheduling</p>
	</header>

	<!-- Setup Guide -->
	<section class="section">
		<button class="setup-toggle mono" onclick={openSetupGuide}>
			{showSetupGuide ? 'HIDE SETUP GUIDE' : 'HOW TO ADD A NODE'}
			<svg width="12" height="12" viewBox="0 0 12 12" fill="none" class="chevron-icon" class:open={showSetupGuide}><path d="M3 4.5l3 3 3-3" stroke="currentColor" stroke-width="1.5"/></svg>
		</button>

		{#if showSetupGuide && setupInfo}
			<div class="setup-guide">
				<!-- Step 1: Generate Token -->
				<div class="setup-step">
					<h3 class="step-title mono">1. GENERATE A NODE TOKEN</h3>
					<p class="step-desc">Each node needs its own auth token. Generate one for the machine you're adding.</p>
					<div class="token-gen-row">
						{#if userOrgs.length > 0}
							<select class="setup-select mono" bind:value={selectedOrgId}>
								{#each userOrgs as org}
									<option value={org.org_id}>{org.name}</option>
								{/each}
							</select>
						{/if}
						<input type="text" class="setup-input" bind:value={tokenLabel} placeholder="Node name (e.g. Render-Box-A)" />
						<button class="btn-setup mono" onclick={generateNodeToken} disabled={tokenGenerating || !tokenLabel.trim()}>
							{tokenGenerating ? '...' : 'GENERATE'}
						</button>
					</div>
					{#if generatedToken}
						<div class="token-result">
							<span class="token-label mono">Token (copy now — shown only once):</span>
							<div class="token-copy-row">
								<input type="text" class="token-value mono" value={generatedToken} readonly />
								<button class="btn-copy mono" onclick={() => navigator.clipboard.writeText(generatedToken)}>COPY</button>
							</div>
						</div>
					{/if}
				</div>

				<!-- Step 2: Run the node -->
				<div class="setup-step">
					<h3 class="step-title mono">2. START THE NODE</h3>

					<div class="code-block">
						<span class="code-label mono">Standalone Binary (Windows & Linux — no Docker needed)</span>
						<div class="download-links">
							<a href="https://github.com/JamesNyeVRGuy/CorridorKey/releases/latest" target="_blank" rel="noopener" class="download-btn mono">
								<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 1v9M4 7l4 4 4-4M2 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
								DOWNLOAD LATEST
							</a>
							<p class="download-hint">Download the installer, paste your token on first launch. Auto-detects your GPU (NVIDIA or AMD) and downloads the right acceleration.</p>
						</div>
					</div>

					<details class="docker-details">
						<summary class="docker-summary mono">Docker Compose (advanced)</summary>

						<p class="step-desc" style="margin-top: var(--sp-3);">Select your GPU type:</p>

						<div class="gpu-vendor-select">
							<button class="vendor-btn mono" class:active={gpuVendor === 'nvidia'} onclick={() => gpuVendor = 'nvidia'}>NVIDIA</button>
							<button class="vendor-btn mono" class:active={gpuVendor === 'amd'} onclick={() => gpuVendor = 'amd'}>AMD</button>
						</div>

						<div class="code-block">
							<span class="code-label mono">Save as docker-compose.yml</span>
						{#if gpuVendor === 'nvidia'}
						<pre class="code mono">services:
  corridorkey-node:
    image: {nodeImage}
    restart: unless-stopped
    labels:
      - com.centurylinklabs.watchtower.enable=true
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    environment:
      - CK_MAIN_URL={setupInfo.main_url}
      - CK_AUTH_TOKEN={generatedToken || '<paste token here>'}
      - CK_NODE_NAME={generatedTokenLabel || 'my-node'}
      - CK_NODE_GPUS=auto
    volumes:
      - ck-weights:/app/CorridorKeyModule/checkpoints
      - ck-weights-gvm:/app/gvm_core/weights
      - ck-weights-vm:/app/VideoMaMaInferenceModule/checkpoints
      - ck-compile-cache:/home/nodeuser/.cache/corridorkey

  watchtower:
    image: containrrr/watchtower
    restart: unless-stopped
    environment:
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=300
      - WATCHTOWER_LABEL_ENABLE=true
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

volumes:
  ck-weights:
  ck-weights-gvm:
  ck-weights-vm:
  ck-compile-cache:</pre>
						{:else}
						<pre class="code mono">services:
  corridorkey-node:
    image: {nodeImage}
    restart: unless-stopped
    labels:
      - com.centurylinklabs.watchtower.enable=true
    devices:
      - /dev/kfd
      - /dev/dri
    security_opt:
      - seccomp=unconfined
    group_add:
      - video
    environment:
      - CK_MAIN_URL={setupInfo.main_url}
      - CK_AUTH_TOKEN={generatedToken || '<paste token here>'}
      - CK_NODE_NAME={generatedTokenLabel || 'my-node'}
      - CK_NODE_GPUS=auto
    volumes:
      - ck-weights:/app/CorridorKeyModule/checkpoints
      - ck-weights-gvm:/app/gvm_core/weights
      - ck-weights-vm:/app/VideoMaMaInferenceModule/checkpoints
      - ck-compile-cache:/home/nodeuser/.cache/corridorkey

  watchtower:
    image: containrrr/watchtower
    restart: unless-stopped
    environment:
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=300
      - WATCHTOWER_LABEL_ENABLE=true
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

volumes:
  ck-weights:
  ck-weights-gvm:
  ck-weights-vm:
  ck-compile-cache:</pre>
						{/if}
						<p class="code-hint mono">docker compose up -d</p>
						</div>
					</details>
				</div>

				<!-- Active Tokens -->
				{#if nodeTokens.length > 0}
					{@const activeTokens = nodeTokens.filter(t => !t.revoked)}
					{@const revokedTokens = nodeTokens.filter(t => t.revoked)}
					{@const orgMap = Object.fromEntries(userOrgs.map(o => [o.org_id, o.name]))}
					{@const orgIds = [...new Set(activeTokens.map(t => t.org_id))]}

					{#each orgIds as oid}
						{@const orgTokens = activeTokens.filter(t => t.org_id === oid)}
						{#if orgTokens.length > 0}
							<div class="setup-step">
								<h3 class="step-title mono">{orgMap[oid] || 'Unknown Org'}</h3>
								<div class="token-list">
									{#each orgTokens as t}
										<div class="token-row">
											<span class="token-preview mono">{t.token_preview}</span>
											<span class="token-name">{t.label}</span>
											{#if t.node_id}
												<span class="token-status mono connected">CONNECTED</span>
											{:else}
												<span class="token-status mono unused">UNUSED</span>
											{/if}
											<button class="btn-revoke mono" onclick={() => revokeToken(t.token_preview)}>REVOKE</button>
										</div>
									{/each}
								</div>
							</div>
						{/if}
					{/each}

					{#if revokedTokens.length > 0}
						<div class="setup-step">
							<button class="revoked-toggle mono" onclick={() => showRevokedTokens = !showRevokedTokens}>
								REVOKED TOKENS ({revokedTokens.length}) {showRevokedTokens ? '▲' : '▼'}
							</button>
							{#if showRevokedTokens}
								<div class="token-list">
									{#each revokedTokens as t}
										<div class="token-row revoked">
											<span class="token-preview mono">{t.token_preview}</span>
											<span class="token-name">{t.label}</span>
											<span class="token-status mono revoked-badge">REVOKED</span>
										</div>
									{/each}
								</div>
							{/if}
						</div>
					{/if}
				{/if}
			</div>
		{/if}
	</section>

	<!-- Local GPU Processing (platform admin only) -->
	{#if isAdmin}
	<section class="section">
		<h2 class="section-title mono">LOCAL GPU PROCESSING</h2>
		<div class="local-gpu-card">
			{#if isAdmin}
			<div class="local-gpu-toggle">
				<div class="toggle-info">
					<span class="toggle-label">Process GPU jobs on this machine</span>
					<span class="toggle-hint mono">
						{#if localGpuEnabled}
							Local + remote nodes will process GPU jobs
						{:else}
							GPU jobs will only run on remote nodes
						{/if}
					</span>
				</div>
				<button class="toggle-btn" class:active={localGpuEnabled} onclick={toggleLocalGpu} role="switch" aria-checked={localGpuEnabled} aria-label="Toggle local GPU processing">
					<span class="toggle-knob"></span>
				</button>
			</div>
			{#if localGpuEnabled}
				<div class="claim-delay-row">
					<div class="claim-delay-info">
						<span class="claim-delay-label">Node priority delay</span>
						<span class="claim-delay-hint mono">
							{#if claimDelay === 0}
								Local claims immediately
							{:else}
								Waits {claimDelay}s for remote nodes first
							{/if}
						</span>
					</div>
					<input
						type="range"
						min="0"
						max="10"
						step="0.5"
						bind:value={claimDelay}
						onchange={() => api.system2.setClaimDelay(claimDelay)}
						class="delay-slider"
					/>
					<span class="delay-val mono">{claimDelay === 0 ? 'OFF' : `${claimDelay}s`}</span>
				</div>
			{/if}
			{/if}
			{#if localGpus.length > 0}
				<div class="local-gpu-list">
					{#each localGpus as gpu}
						<div class="gpu-row-local">
							<span class="gpu-index mono">GPU {gpu.index}</span>
							<span class="gpu-name">{gpu.name}</span>
							<div class="gpu-slot-vram">
								<div class="vram-bar small">
									<div
										class="vram-used"
										style="width: {gpu.vram_total_gb > 0 ? ((gpu.vram_total_gb - gpu.vram_free_gb) / gpu.vram_total_gb) * 100 : 0}%"
									></div>
								</div>
								<span class="vram-label mono"
									>{gpu.vram_free_gb.toFixed(1)} / {gpu.vram_total_gb.toFixed(1)} GB</span
								>
							</div>
						</div>
					{/each}
				</div>
			{/if}
			{#if localCpu}
				<div class="cpu-stats-row">
					<span class="cpu-label mono">CPU</span>
					<span class="cpu-detail mono">{localCpu.cpu_count} cores</span>
					<div class="cpu-bar-wrap">
						<div class="vram-bar small">
							<div class="vram-used cpu-fill" style="width: {localCpu.cpu_percent}%"></div>
						</div>
						<span class="vram-label mono">{localCpu.cpu_percent}%</span>
					</div>
					<span class="cpu-label mono">RAM</span>
					<div class="cpu-bar-wrap">
						<div class="vram-bar small">
							<div class="vram-used" style="width: {localCpu.ram_total_gb > 0 ? (localCpu.ram_used_gb / localCpu.ram_total_gb) * 100 : 0}%"></div>
						</div>
						<span class="vram-label mono">{localCpu.ram_free_gb.toFixed(1)} / {localCpu.ram_total_gb.toFixed(1)} GB</span>
					</div>
				</div>
			{/if}
		</div>
	</section>
	{/if}

	<!-- Remote Nodes -->
	<section class="section">
		<h2 class="section-title mono">
			REMOTE NODES
			{#if $nodes.length > 0}
				<span class="count-badge mono">{$nodes.length}</span>
			{/if}
		</h2>

		{#if $nodes.length === 0}
			<div class="empty-state">
				<p class="mono">No remote nodes connected</p>
				<p class="step-desc">Use the setup guide above to add a render node.</p>
			</div>
		{:else}
			<div class="node-list">
				{#each $nodes as node (node.node_id)}
					<div
						class="node-card"
						class:offline={node.status === 'offline'}
						class:paused={node.paused}
					>
						<div class="node-header">
							<span class="node-dot {statusClass(node.status)}"></span>
							<span class="node-name">{node.name}</span>
							{#if node.org_name}
								<span class="node-org mono">{node.org_name}</span>
							{/if}
							{#if node.can_manage}
								<button
									class="visibility-badge mono {node.visibility}"
									onclick={() => {
										const next = node.visibility === 'shared' ? 'private' : 'shared';
										api.nodes.setVisibility(node.node_id, next).then(refreshNodes);
									}}
									title="Click to toggle visibility"
								>{node.visibility === 'shared' ? 'SHARED' : 'PRIVATE'}</button>
							{:else if node.visibility === 'shared'}
								<span class="visibility-badge mono shared">SHARED</span>
							{/if}
							{#if node.reputation}
								<span class="rep-wrapper">
									<button class="rep-badge mono" class:rep-good={node.reputation.score >= 70} class:rep-mid={node.reputation.score >= 40 && node.reputation.score < 70} class:rep-bad={node.reputation.score < 40} aria-label="Reputation score breakdown" onclick={(e) => { e.stopPropagation(); showRepBreakdown = showRepBreakdown === node.node_id ? null : node.node_id; }}>
										{node.reputation.score}
									</button>
									{#if showRepBreakdown === node.node_id && node.reputation.breakdown}
										<div class="rep-breakdown">
											<div class="rep-row"><span>Success rate</span><span class="mono">{(node.reputation.breakdown.success.value * 100).toFixed(0)}%</span><span class="mono rep-pts">{node.reputation.breakdown.success.points}/50</span></div>
											<div class="rep-row"><span>Speed</span><span class="mono">{node.reputation.breakdown.speed.value} fps</span><span class="mono rep-pts">{node.reputation.breakdown.speed.points}/20</span></div>
											<div class="rep-row"><span>Uptime</span><span class="mono">{(node.reputation.breakdown.uptime.value * 100).toFixed(0)}%</span><span class="mono rep-pts">{node.reputation.breakdown.uptime.points}/30</span></div>
											{#if node.reputation.breakdown.security_penalty.warnings > 0}
												<div class="rep-row rep-penalty"><span>Security</span><span class="mono">{node.reputation.breakdown.security_penalty.warnings} warnings</span><span class="mono rep-pts">{node.reputation.breakdown.security_penalty.points}</span></div>
											{/if}
											<div class="rep-divider"></div>
											<div class="rep-row"><span>Jobs</span><span class="mono">{node.reputation.stats.completed_jobs} done, {node.reputation.stats.failed_jobs} failed</span></div>
											<div class="rep-row"><span>Frames</span><span class="mono">{node.reputation.stats.total_frames.toLocaleString()}</span></div>
										</div>
									{/if}
								</span>
							{/if}
							{#if node.version_match === false}
								<span class="node-badge outdated mono" title="Node version differs from server — update recommended">OUTDATED</span>
							{/if}
							{#if node.paused}
								<span class="node-badge paused mono">PAUSED</span>
							{:else if node.schedule.enabled && !node.schedule.is_active_now}
								<span class="node-badge scheduled mono">SCHEDULED</span>
							{:else}
								<span class="node-status mono {statusClass(node.status)}"
									>{node.status.toUpperCase()}</span
								>
							{/if}
							{#if node.can_manage}
							<div class="node-actions">
								<button
									class="btn-icon"
									title={node.paused ? 'Resume' : 'Pause'}
									onclick={() => togglePause(node)}
								>
									{#if node.paused}
										<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
											><path d="M5 3l8 5-8 5V3z" fill="currentColor" /></svg
										>
									{:else}
										<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
											><rect x="3" y="3" width="3.5" height="10" rx="0.5" fill="currentColor" /><rect
												x="9.5"
												y="3"
												width="3.5"
												height="10"
												rx="0.5"
												fill="currentColor"
											/></svg
										>
									{/if}
								</button>
								<button
									class="btn-icon"
									title="Schedule"
									onclick={() => openScheduleEditor(node)}
								>
									<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
										><circle
											cx="8"
											cy="8"
											r="5.5"
											stroke="currentColor"
											stroke-width="1.2"
										/><path
											d="M8 4.5V8l2.5 1.5"
											stroke="currentColor"
											stroke-width="1.2"
											stroke-linecap="round"
										/></svg
									>
								</button>
								<button
									class="btn-icon"
									class:active={viewingLogs === node.node_id}
									title="View logs"
									onclick={() => toggleLogs(node.node_id)}
								>
									<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M3 4h10M3 7h8M3 10h6M3 13h9" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
								</button>
								<button
									class="btn-icon"
									class:active={viewingHealth === node.node_id}
									title="Health history"
									onclick={() => toggleHealth(node.node_id)}
								>
									<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><polyline points="1,12 4,4 7,9 10,2 13,8 16,5" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
								</button>
								<button
									class="btn-icon"
									title="Job types"
									onclick={() => openTypesEditor(node)}
								>
									<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 3h12M2 3l4 5v4l2 1V8l4-5" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>
								</button>
								<button
									class="btn-icon danger"
									title="Remove node"
									onclick={() => removeNode(node.node_id)}
								>
									<svg width="14" height="14" viewBox="0 0 16 16" fill="none">
										<path
											d="M4 4l8 8M12 4l-8 8"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
										/>
									</svg>
								</button>
							</div>
							{/if}
						</div>

						<!-- Schedule editor (inline, management only) -->
						{#if node.can_manage && editingSchedule === node.node_id}
							<div class="schedule-editor">
								<div class="schedule-row">
									<label class="schedule-toggle">
										<input type="checkbox" bind:checked={scheduleEnabled} />
										<span>Active hours</span>
									</label>
								</div>
								{#if scheduleEnabled}
									<div class="schedule-times">
										<label class="time-field">
											<span class="time-label mono">FROM</span>
											<input type="time" bind:value={scheduleStart} />
										</label>
										<span class="time-sep">—</span>
										<label class="time-field">
											<span class="time-label mono">TO</span>
											<input type="time" bind:value={scheduleEnd} />
										</label>
									</div>
									<p class="schedule-hint mono">
										{#if scheduleStart > scheduleEnd}
											Overnight: active from {scheduleStart} to {scheduleEnd} next day
										{:else}
											Active from {scheduleStart} to {scheduleEnd}
										{/if}
									</p>
								{/if}
								<div class="schedule-actions">
									<button class="btn-save" onclick={saveSchedule}>Save</button>
									<button class="btn-cancel" onclick={cancelScheduleEdit}>Cancel</button>
								</div>
							</div>
						{/if}

						<!-- Job types editor (inline, management only) -->
						{#if node.can_manage && editingTypes === node.node_id}
							<div class="schedule-editor">
								<p class="types-hint mono">Select which job types this node accepts. None selected = all types.</p>
								<div class="types-grid">
									{#each ALL_JOB_TYPES as jt}
										<label class="type-chip {jt.kind}" class:selected={selectedTypes.has(jt.value)}>
											<input
												type="checkbox"
												checked={selectedTypes.has(jt.value)}
												onchange={() => toggleType(jt.value)}
											/>
											<span>{jt.label}</span>
										</label>
									{/each}
								</div>
								<div class="schedule-actions">
									<button class="btn-save" onclick={saveTypes}>Save</button>
									<button class="btn-cancel" onclick={cancelTypesEdit}>Cancel</button>
								</div>
							</div>
						{/if}

						<!-- Log viewer (org admin only) -->
						{#if node.can_manage && viewingLogs === node.node_id}
							<div class="node-logs">
								{#if logLines.length === 0}
									<p class="log-empty mono">No logs yet</p>
								{:else}
									<pre class="log-output mono">{logLines.join('\n')}</pre>
								{/if}
							</div>
						{/if}

						<!-- Health graph (org admin only) -->
						{#if node.can_manage && viewingHealth === node.node_id}
							<div class="node-health">
								{#if healthData.length < 2}
									<p class="log-empty mono">Not enough data yet (need 2+ heartbeats)</p>
								{:else}
									<div class="health-legend mono">
										<span class="legend-cpu">CPU</span>
										<span class="legend-ram">RAM</span>
										<span class="legend-period">{healthData.length} samples (~{Math.round(healthData.length * 10 / 60)}min)</span>
									</div>
									<canvas bind:this={healthCanvas} class="health-canvas"></canvas>
								{/if}
							</div>
						{/if}

						{#if node.gpus && node.gpus.length > 0}
							<div class="node-gpus">
								{#each node.gpus as gpu}
									<div class="gpu-row">
										<span class="gpu-slot-dot" class:busy={gpu.status === 'busy'}></span>
										<span class="gpu-slot-index mono">GPU {gpu.index}</span>
										<span class="gpu-slot-name">{gpu.name}</span>
										<div class="gpu-slot-vram">
											<div class="vram-bar small">
												<div
													class="vram-used"
													style="width: {gpu.vram_total_gb > 0 ? ((gpu.vram_total_gb - gpu.vram_free_gb) / gpu.vram_total_gb) * 100 : 0}%"
												></div>
											</div>
											<span class="vram-label mono">{gpu.vram_free_gb.toFixed(1)}G</span>
										</div>
										{#if gpu.current_job_id}
											<span class="gpu-job mono">{gpu.current_job_id}</span>
										{/if}
									</div>
								{/each}
							</div>
						{:else if node.gpu_name}
							<div class="node-gpu-legacy">
								<span class="gpu-name">{node.gpu_name}</span>
								<span class="vram-label mono"
									>{node.vram_free_gb.toFixed(1)} / {node.vram_total_gb.toFixed(1)} GB</span
								>
							</div>
						{/if}

						{#if node.cpu_stats}
							<div class="cpu-stats-row node-cpu">
								<span class="cpu-label mono">CPU</span>
								<div class="cpu-bar-wrap">
									<div class="vram-bar small">
										<div class="vram-used cpu-fill" style="width: {node.cpu_stats.cpu_percent}%"></div>
									</div>
									<span class="vram-label mono">{node.cpu_stats.cpu_percent}%</span>
								</div>
								<span class="cpu-label mono">RAM</span>
								<div class="cpu-bar-wrap">
									<div class="vram-bar small">
										<div class="vram-used" style="width: {node.cpu_stats.ram_total_gb > 0 ? (node.cpu_stats.ram_used_gb / node.cpu_stats.ram_total_gb) * 100 : 0}%"></div>
									</div>
									<span class="vram-label mono">{node.cpu_stats.ram_free_gb.toFixed(1)}G</span>
								</div>
							</div>
						{/if}

						<div class="node-footer">
							<span class="node-caps mono">{node.capabilities.join(', ')}</span>
							{#if node.shared_storage}
								<span class="node-tag shared mono" title={node.shared_storage}>SHARED</span>
							{/if}
							{#if node.schedule.enabled}
								<span class="node-tag schedule mono" title="Active hours">
									{formatSchedule(node)}
								</span>
							{/if}
							{#if node.accepted_types && node.accepted_types.length > 0}
								<span class="node-tag types mono" title="Accepted job types">
									{formatTypes(node)}
								</span>
							{/if}
							<span class="node-heartbeat mono">{timeSince(node.last_heartbeat)}</span>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</section>
</div>

<style>
	.page {
		padding: var(--sp-6);
		max-width: 900px;
	}

	.page-header {
		margin-bottom: var(--sp-6);
	}

	.page-header h1 {
		font-size: 20px;
		font-weight: 600;
		color: var(--text-primary);
		letter-spacing: 0.1em;
	}

	.subtitle {
		color: var(--text-tertiary);
		font-size: 13px;
		margin-top: var(--sp-1);
	}

	.section {
		margin-bottom: var(--sp-8);
	}

	.section-title {
		font-size: 11px;
		font-weight: 600;
		color: var(--text-tertiary);
		letter-spacing: 0.15em;
		margin-bottom: var(--sp-3);
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.count-badge {
		font-size: 9px;
		background: var(--accent);
		color: #000;
		padding: 1px 6px;
		border-radius: 8px;
		font-weight: 700;
	}

	/* Local GPU card */
	.local-gpu-card {
		background: var(--surface-1);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: var(--sp-3) var(--sp-4);
	}

	.local-gpu-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--sp-3);
	}

	.toggle-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.toggle-label {
		font-size: 14px;
		font-weight: 500;
		color: var(--text-primary);
	}

	.toggle-hint {
		font-size: 11px;
		color: var(--text-tertiary);
	}

	.claim-delay-row {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
		margin-top: var(--sp-2);
		padding-top: var(--sp-2);
		border-top: 1px solid var(--border-subtle);
	}

	.claim-delay-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 160px;
	}

	.claim-delay-label {
		font-size: 12px;
		color: var(--text-secondary);
	}

	.claim-delay-hint {
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.delay-slider {
		flex: 1;
		-webkit-appearance: none;
		appearance: none;
		height: 4px;
		background: var(--surface-4);
		border-radius: 2px;
		outline: none;
		cursor: pointer;
	}

	.delay-slider::-webkit-slider-thumb {
		-webkit-appearance: none;
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--accent);
		cursor: pointer;
		border: 2px solid var(--surface-2);
	}

	.delay-slider::-moz-range-thumb {
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--accent);
		cursor: pointer;
		border: 2px solid var(--surface-2);
	}

	.delay-val {
		font-size: 12px;
		font-weight: 600;
		color: var(--accent);
		min-width: 32px;
		text-align: right;
	}

	.toggle-btn {
		width: 40px;
		height: 22px;
		border-radius: 11px;
		border: none;
		background: var(--surface-4);
		cursor: pointer;
		position: relative;
		transition: background 0.2s;
		flex-shrink: 0;
	}

	.toggle-btn.active {
		background: var(--accent);
	}

	.toggle-knob {
		position: absolute;
		top: 3px;
		left: 3px;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--text-primary);
		transition: transform 0.2s;
	}

	.toggle-btn.active .toggle-knob {
		transform: translateX(18px);
		background: #000;
	}

	.local-gpu-list {
		margin-top: var(--sp-3);
		padding-top: var(--sp-2);
		border-top: 1px solid var(--border-subtle);
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.gpu-row-local {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		font-size: 12px;
	}

	.empty-state {
		color: var(--text-tertiary);
		padding: var(--sp-6) var(--sp-4);
		text-align: center;
		border: 1px dashed var(--border);
		border-radius: 6px;
	}

	.instructions {
		margin-top: var(--sp-3);
		font-size: 13px;
	}

	.instructions code {
		display: block;
		margin-top: var(--sp-2);
		padding: var(--sp-2) var(--sp-3);
		background: var(--surface-2);
		border-radius: 4px;
		font-size: 12px;
		color: var(--accent);
		word-break: break-all;
	}

	/* Node list */
	.node-list {
		display: flex;
		flex-direction: column;
		gap: var(--sp-2);
	}

	.node-card {
		background: var(--surface-1);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: var(--sp-3) var(--sp-4);
		transition: border-color 0.2s;
	}

	.node-card:hover {
		border-color: var(--border-active);
	}

	.node-card.offline {
		opacity: 0.5;
	}

	.node-card.paused {
		border-left: 3px solid var(--state-queued);
	}

	.node-header {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.node-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.status-online {
		background: var(--state-complete);
		box-shadow: 0 0 6px rgba(93, 216, 121, 0.4);
	}
	.status-busy {
		background: var(--accent);
		box-shadow: 0 0 6px rgba(255, 242, 3, 0.4);
	}
	.status-offline {
		background: var(--state-error);
	}

	.node-name {
		font-size: 14px;
		font-weight: 500;
		color: var(--text-primary);
	}

	.node-host {
		font-size: 11px;
		color: var(--text-tertiary);
	}

	.node-status {
		margin-left: auto;
		font-size: 9px;
		letter-spacing: 0.08em;
		padding: 1px 6px;
		border: 1px solid currentColor;
		border-radius: 3px;
	}

	.node-badge {
		margin-left: auto;
		font-size: 9px;
		letter-spacing: 0.08em;
		padding: 1px 6px;
		border: 1px solid currentColor;
		border-radius: 3px;
	}

	.node-badge.paused {
		color: var(--state-queued);
	}

	.node-badge.scheduled {
		color: var(--secondary);
	}

	.node-badge.outdated {
		color: var(--state-raw);
		border-color: rgba(240, 160, 48, 0.3);
		background: rgba(240, 160, 48, 0.08);
	}

	.node-actions {
		display: flex;
		align-items: center;
		gap: 2px;
		margin-left: var(--sp-1);
	}

	.btn-icon {
		background: none;
		border: none;
		color: var(--text-tertiary);
		cursor: pointer;
		padding: 3px;
		border-radius: 3px;
		display: flex;
		align-items: center;
	}

	.btn-icon:hover {
		color: var(--text-primary);
		background: var(--surface-3);
	}

	.btn-icon.danger:hover {
		color: var(--state-error);
		background: rgba(255, 82, 82, 0.1);
	}

	/* Schedule editor */
	.schedule-editor {
		margin-top: var(--sp-2);
		padding: var(--sp-3);
		background: var(--surface-2);
		border-radius: 4px;
		display: flex;
		flex-direction: column;
		gap: var(--sp-2);
	}

	.schedule-row {
		display: flex;
		align-items: center;
	}

	.schedule-toggle {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		font-size: 13px;
		color: var(--text-primary);
		cursor: pointer;
	}

	.schedule-toggle input {
		accent-color: var(--accent);
	}

	.schedule-times {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.time-field {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.time-label {
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.time-field input[type='time'] {
		background: var(--surface-3);
		border: 1px solid var(--border);
		border-radius: 3px;
		color: var(--text-primary);
		padding: 3px 6px;
		font-size: 13px;
		font-family: var(--font-mono);
	}

	.time-sep {
		color: var(--text-tertiary);
	}

	.schedule-hint {
		font-size: 11px;
		color: var(--text-tertiary);
	}

	.schedule-actions {
		display: flex;
		gap: var(--sp-2);
	}

	.btn-save {
		background: var(--accent);
		color: #000;
		border: none;
		border-radius: 4px;
		padding: 4px 14px;
		font-size: 12px;
		font-weight: 600;
		cursor: pointer;
	}

	.btn-save:hover {
		background: var(--accent-dim);
	}

	.btn-cancel {
		background: none;
		color: var(--text-tertiary);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 4px 14px;
		font-size: 12px;
		cursor: pointer;
	}

	.btn-cancel:hover {
		color: var(--text-primary);
		border-color: var(--text-tertiary);
	}

	/* Multi-GPU rows */
	.node-gpus {
		margin-top: var(--sp-2);
		padding-left: 18px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.gpu-row {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		font-size: 12px;
	}

	.gpu-slot-dot {
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: var(--state-complete);
		flex-shrink: 0;
	}

	.gpu-slot-dot.busy {
		background: var(--accent);
	}

	.gpu-slot-index {
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.gpu-slot-name {
		color: var(--text-secondary);
		font-size: 12px;
	}

	.gpu-slot-vram {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-left: auto;
	}

	.vram-bar {
		flex: 1;
		height: 4px;
		background: var(--surface-3);
		border-radius: 2px;
		overflow: hidden;
	}

	.vram-bar.small {
		max-width: 60px;
	}

	.vram-used {
		height: 100%;
		background: var(--accent);
		border-radius: 2px;
		transition: width 0.3s;
	}

	.vram-label {
		font-size: 10px;
		color: var(--text-tertiary);
		white-space: nowrap;
	}

	.gpu-index {
		font-size: 10px;
		color: var(--accent);
		font-weight: 600;
	}

	.gpu-name {
		font-size: 13px;
		color: var(--text-secondary);
	}

	.gpu-job {
		font-size: 10px;
		color: var(--accent);
		padding: 0 4px;
		background: var(--accent-muted);
		border-radius: 3px;
	}

	/* Legacy single GPU */
	.node-gpu-legacy {
		margin-top: var(--sp-2);
		padding-left: 18px;
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		font-size: 12px;
	}

	.node-footer {
		margin-top: var(--sp-2);
		display: flex;
		align-items: center;
		gap: var(--sp-3);
		padding-top: var(--sp-2);
		border-top: 1px solid var(--border-subtle);
	}

	.node-caps {
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.node-tag {
		font-size: 9px;
		padding: 0 4px;
		border: 1px solid currentColor;
		border-radius: 3px;
	}

	.node-tag.shared {
		color: var(--state-complete);
	}

	.node-tag.schedule {
		color: var(--secondary);
	}

	.node-tag.types {
		color: var(--state-raw);
	}

	.types-hint {
		font-size: 11px;
		color: var(--text-tertiary);
	}

	.types-grid {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	.type-chip {
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: 12px;
		color: var(--text-secondary);
		padding: 3px 8px;
		border: 1px solid var(--border);
		border-radius: 4px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.type-chip:hover {
		border-color: var(--text-tertiary);
	}

	.type-chip.selected.gpu {
		border-color: var(--accent);
		color: var(--accent);
		background: var(--accent-muted);
	}

	.type-chip.selected.cpu {
		border-color: var(--secondary);
		color: var(--secondary);
		background: var(--secondary-muted);
	}

	.type-chip.gpu::before {
		content: '';
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--accent-dim);
		flex-shrink: 0;
	}

	.type-chip.cpu::before {
		content: '';
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--secondary);
		flex-shrink: 0;
		opacity: 0.6;
	}

	.type-chip input {
		display: none;
	}

	.cpu-stats-row {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		font-size: 11px;
		padding-top: var(--sp-2);
		border-top: 1px solid var(--border-subtle);
		margin-top: var(--sp-2);
	}

	.cpu-stats-row.node-cpu {
		padding-left: 18px;
	}

	.cpu-label {
		font-size: 10px;
		color: var(--text-tertiary);
		font-weight: 600;
	}

	.cpu-detail {
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.cpu-bar-wrap {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.cpu-fill {
		background: var(--secondary) !important;
	}

	.node-health {
		margin-top: var(--sp-2);
	}

	.health-legend {
		display: flex;
		gap: var(--sp-3);
		font-size: 10px;
		margin-bottom: var(--sp-1);
	}

	.legend-cpu {
		color: var(--secondary);
	}

	.legend-ram {
		color: var(--accent);
	}

	.legend-period {
		margin-left: auto;
		color: var(--text-tertiary);
	}

	.health-canvas {
		width: 100%;
		height: 40px;
		background: var(--surface-0);
		border: 1px solid var(--border);
		border-radius: 4px;
	}

	.btn-icon.active {
		color: var(--accent);
		background: var(--accent-muted);
	}

	.node-logs {
		margin-top: var(--sp-2);
	}

	.log-empty {
		font-size: 11px;
		color: var(--text-tertiary);
		padding: var(--sp-2);
	}

	.log-output {
		font-size: 10px;
		color: var(--text-secondary);
		background: var(--surface-0);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: var(--sp-3);
		max-height: 300px;
		overflow: auto;
		white-space: pre-wrap;
		word-break: break-all;
		line-height: 1.5;
	}

	.node-heartbeat {
		margin-left: auto;
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.node-org { font-size: 10px; color: var(--text-tertiary); letter-spacing: 0.06em; }

	.rep-wrapper { position: relative; display: inline-block; }
	.rep-badge {
		font-size: 10px; font-weight: 700; padding: 1px 6px;
		border-radius: 3px; letter-spacing: 0.04em;
		border: none; cursor: pointer; font-family: inherit;
	}
	.rep-good { background: rgba(93, 216, 121, 0.12); color: var(--state-complete); }
	.rep-mid { background: rgba(255, 242, 3, 0.12); color: var(--accent); }
	.rep-bad { background: rgba(255, 82, 82, 0.12); color: var(--state-error); }
	.rep-breakdown {
		position: absolute; top: 100%; right: 0; z-index: 100;
		background: var(--surface-2); border: 1px solid var(--border);
		border-radius: var(--radius-md); padding: var(--sp-3);
		min-width: 260px; margin-top: 4px;
		box-shadow: 0 4px 16px rgba(0,0,0,0.4);
	}
	.rep-row {
		display: grid; grid-template-columns: 80px 1fr 50px;
		gap: var(--sp-2); align-items: baseline;
		font-size: 11px; padding: 3px 0; color: var(--text-secondary);
	}
	.rep-row span:first-child { color: var(--text-tertiary); }
	.rep-pts { color: var(--accent); text-align: right; }
	.rep-penalty .rep-pts { color: var(--state-error); }
	.rep-divider { border-top: 1px solid var(--border); margin: 6px 0; }

	.visibility-badge {
		font-size: 9px; letter-spacing: 0.06em; padding: 2px 6px;
		border-radius: 3px; border: none; cursor: default;
	}
	button.visibility-badge { cursor: pointer; transition: all 0.15s; }
	button.visibility-badge:hover { opacity: 0.8; }
	.visibility-badge.shared { background: rgba(0, 154, 218, 0.12); color: var(--secondary); }
	.visibility-badge.private { background: rgba(117, 117, 117, 0.12); color: var(--state-cancelled); }

	/* Setup guide */
	.setup-toggle {
		display: flex; align-items: center; gap: var(--sp-2);
		font-size: 12px; letter-spacing: 0.08em; color: var(--accent);
		background: none; border: 1px solid var(--accent-dim);
		border-radius: var(--radius-md); padding: 8px 16px;
		cursor: pointer; transition: all 0.15s;
	}
	.setup-toggle:hover { background: var(--accent-muted); }
	.chevron-icon { transition: transform 0.2s; }
	.chevron-icon.open { transform: rotate(180deg); }

	.setup-guide {
		display: flex; flex-direction: column; gap: var(--sp-5);
		margin-top: var(--sp-4); padding: var(--sp-5);
		background: var(--surface-2); border: 1px solid var(--border);
		border-radius: var(--radius-lg);
	}

	.setup-step { display: flex; flex-direction: column; gap: var(--sp-3); }
	.step-title { font-size: 11px; letter-spacing: 0.1em; color: var(--accent); }
	.step-desc { font-size: 13px; color: var(--text-secondary); line-height: 1.4; }

	.token-gen-row { display: flex; gap: var(--sp-2); align-items: center; }
	.setup-input {
		flex: 1; padding: 8px 12px; background: var(--surface-3);
		border: 1px solid var(--border); border-radius: var(--radius-sm);
		color: var(--text-primary); font-size: 13px; outline: none;
	}
	.setup-input:focus { border-color: var(--accent); }
	.setup-input::placeholder { color: var(--text-tertiary); }
	.setup-select {
		padding: 8px 10px; background: var(--surface-3);
		border: 1px solid var(--border); border-radius: var(--radius-sm);
		color: var(--text-secondary); font-size: 12px; cursor: pointer; outline: none;
	}
	.btn-setup {
		padding: 8px 14px; background: var(--accent); color: #000;
		font-weight: 600; font-size: 11px; border: none;
		border-radius: var(--radius-sm); cursor: pointer; flex-shrink: 0;
	}
	.btn-setup:disabled { opacity: 0.4; cursor: not-allowed; }
	.btn-setup:hover:not(:disabled) { background: #fff; }

	.token-result {
		display: flex; flex-direction: column; gap: var(--sp-2);
		padding: var(--sp-3); background: rgba(93, 216, 121, 0.06);
		border: 1px solid rgba(93, 216, 121, 0.2); border-radius: var(--radius-md);
	}
	.token-label { font-size: 11px; color: var(--state-complete); }
	.token-copy-row { display: flex; gap: var(--sp-2); }
	.token-value {
		flex: 1; padding: 6px 10px; background: var(--surface-3);
		border: 1px solid var(--border); border-radius: var(--radius-sm);
		color: var(--text-primary); font-size: 11px; outline: none;
	}
	.btn-copy {
		padding: 6px 10px; font-size: 10px; letter-spacing: 0.06em;
		background: none; border: 1px solid var(--accent-dim);
		border-radius: var(--radius-sm); color: var(--accent); cursor: pointer;
	}
	.btn-copy:hover { background: var(--accent-muted); }

	.gpu-vendor-select {
		display: flex; gap: var(--sp-2); margin-bottom: var(--sp-3);
	}
	.vendor-btn {
		flex: 1; padding: 8px; font-size: 11px; letter-spacing: 0.08em;
		background: var(--surface-2); border: 1px solid var(--border);
		border-radius: var(--radius-sm); color: var(--text-secondary);
		cursor: pointer; transition: all 0.15s;
	}
	.vendor-btn:hover { border-color: var(--text-tertiary); color: var(--text-primary); }
	.vendor-btn.active {
		background: var(--accent-muted); border-color: var(--accent);
		color: var(--accent); font-weight: 600;
	}
	.docker-details {
		margin-top: var(--sp-4);
		border: 1px solid var(--border); border-radius: var(--radius-sm);
		padding: var(--sp-3);
	}
	.docker-summary {
		font-size: 10px; letter-spacing: 0.08em; color: var(--text-tertiary);
		cursor: pointer; user-select: none;
	}
	.docker-summary:hover { color: var(--text-secondary); }
	.docker-details[open] .docker-summary { color: var(--text-secondary); margin-bottom: var(--sp-2); }
	.download-links {
		padding: var(--sp-3); background: var(--surface-0);
		border: 1px solid var(--border); border-radius: var(--radius-sm);
	}
	.download-btn {
		display: inline-flex; align-items: center; gap: var(--sp-2);
		font-size: 11px; letter-spacing: 0.06em; font-weight: 600;
		padding: 8px 16px; background: var(--accent); color: #000;
		border-radius: var(--radius-sm); transition: all 0.15s;
	}
	.download-btn:hover { background: #fff; box-shadow: 0 0 12px rgba(255, 242, 3, 0.2); }
	.download-hint {
		font-size: 10px; color: var(--text-tertiary); margin-top: var(--sp-2);
	}
	.code-block {
		display: flex; flex-direction: column; gap: 4px;
	}
	.code-label { font-size: 10px; color: var(--text-tertiary); letter-spacing: 0.08em; }
	.code {
		padding: var(--sp-3); background: var(--surface-0);
		border: 1px solid var(--border); border-radius: var(--radius-sm);
		font-size: 11px; color: var(--text-secondary); overflow-x: auto;
		white-space: pre; line-height: 1.6;
	}

	.token-org-group { margin-bottom: var(--sp-4); }
	.token-org-label {
		display: block; font-size: 10px; letter-spacing: 0.08em;
		color: var(--accent); margin-bottom: var(--sp-2);
		padding-bottom: var(--sp-1); border-bottom: 1px solid var(--border);
	}
	.revoked-toggle {
		background: none; border: none; color: var(--text-tertiary);
		font-size: 10px; letter-spacing: 0.08em; cursor: pointer;
		padding: var(--sp-2) 0; text-align: left; width: 100%;
	}
	.revoked-toggle:hover { color: var(--text-secondary); }
	.token-list { display: flex; flex-direction: column; gap: var(--sp-2); }
	.token-row {
		display: flex; align-items: center; gap: var(--sp-3);
		padding: var(--sp-2) var(--sp-3); background: var(--surface-3);
		border-radius: var(--radius-sm);
	}
	.token-row.revoked { opacity: 0.5; }
	.token-preview { font-size: 11px; color: var(--text-tertiary); min-width: 80px; }
	.token-name { flex: 1; font-size: 13px; color: var(--text-primary); }
	.token-status { font-size: 9px; letter-spacing: 0.06em; padding: 2px 6px; border-radius: 3px; }
	.token-status.connected { background: rgba(93, 216, 121, 0.12); color: var(--state-complete); }
	.token-status.unused { background: rgba(255, 242, 3, 0.12); color: var(--accent); }
	.token-status.revoked-badge { background: rgba(117, 117, 117, 0.12); color: var(--state-cancelled); }
	.btn-revoke {
		font-size: 9px; letter-spacing: 0.06em; padding: 2px 8px;
		background: none; border: 1px solid rgba(255, 82, 82, 0.3);
		border-radius: 3px; color: var(--state-error); cursor: pointer;
	}
	.btn-revoke:hover { background: rgba(255, 82, 82, 0.1); }
</style>
