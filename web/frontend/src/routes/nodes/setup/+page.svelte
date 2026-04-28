<script lang="ts">
	import { onMount } from 'svelte';
	import { getActiveOrgId } from '$lib/auth';

	let step = $state(1);
	let gpuVendor = $state<'nvidia' | 'amd'>('nvidia');
	let setupInfo = $state<{ main_url: string; image: string } | null>(null);
	let nodeImage = $derived(setupInfo ? setupInfo.image.replace(/:[\w.-]+$/, `:${gpuVendor}`) : '');

	// Token generation
	let userOrgs = $state<{ org_id: string; name: string }[]>([]);
	let selectedOrgId = $state('');
	let tokenLabel = $state('');
	let tokenGenerating = $state(false);
	let generatedToken = $state('');
	let generatedTokenLabel = $state('');

	// Existing tokens
	let nodeTokens = $state<{ token_preview: string; label: string; org_id: string; node_id: string | null; revoked: boolean; created_at: number }[]>([]);
	let showRevokedTokens = $state(false);

	async function authFetch(path: string, opts?: RequestInit) {
		const token = localStorage.getItem('ck:auth_token');
		const headers: Record<string, string> = { 'Content-Type': 'application/json' };
		if (token) headers['Authorization'] = `Bearer ${token}`;
		return fetch(path, { ...opts, headers }).then(r => r.json());
	}

	async function generateToken() {
		if (!tokenLabel.trim() || !selectedOrgId) return;
		tokenGenerating = true; generatedToken = '';
		try {
			const res = await fetch('/api/farm/tokens', {
				method: 'POST',
				headers: { 'Authorization': `Bearer ${localStorage.getItem('ck:auth_token')}`, 'Content-Type': 'application/json' },
				body: JSON.stringify({ org_id: selectedOrgId, label: tokenLabel.trim() }),
			});
			const data = await res.json();
			if (res.ok) {
				generatedToken = data.token;
				generatedTokenLabel = tokenLabel.trim();
				tokenLabel = '';
				const tokensRes = await authFetch('/api/farm/tokens');
				nodeTokens = tokensRes?.tokens ?? [];
				step = 3;
			}
		} catch { /* ignore */ }
		finally { tokenGenerating = false; }
	}

	async function revokeToken(preview: string) {
		const prefix = preview.replace(/\.+$/, '');
		nodeTokens = nodeTokens.map(t => t.token_preview === preview ? { ...t, revoked: true } : t);
		await fetch(`/api/farm/tokens/${encodeURIComponent(prefix)}`, {
			method: 'DELETE', headers: { 'Authorization': `Bearer ${localStorage.getItem('ck:auth_token')}` },
		});
	}

	function copyToClipboard(text: string) {
		navigator.clipboard.writeText(text);
	}

	onMount(async () => {
		try {
			const [setup, orgsRes, tokensRes] = await Promise.all([
				authFetch('/api/farm/setup'),
				authFetch('/api/orgs'),
				authFetch('/api/farm/tokens'),
			]);
			setupInfo = setup;
			userOrgs = orgsRes?.orgs ?? [];
			nodeTokens = tokensRes?.tokens ?? [];
			if (userOrgs.length > 0 && !selectedOrgId) selectedOrgId = userOrgs[0].org_id;
		} catch { /* ignore */ }
	});

	let composeText = $derived.by(() => {
		if (!setupInfo) return '';
		const token = generatedToken || '<paste token here>';
		const name = generatedTokenLabel || 'my-node';
		const watchtower = `
  # Auto-updater — checks for new node images every hour
  watchtower:
    image: nickfedor/watchtower
    restart: unless-stopped
    environment:
      - WATCHTOWER_LABEL_ENABLE=true
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=3600
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock`;

		if (gpuVendor === 'nvidia') {
			return `services:
  corridorkey-node:
    image: ${nodeImage}
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
      - CK_MAIN_URL=${setupInfo.main_url}
      - CK_AUTH_TOKEN=${token}
      - CK_NODE_NAME=${name}
      - CK_NODE_GPUS=auto
    volumes:
      - ck-weights:/app/CorridorKeyModule/checkpoints
      - ck-weights-gvm:/app/gvm_core/weights
      - ck-weights-vm:/app/VideoMaMaInferenceModule/checkpoints
      - ck-compile-cache:/app/.cache/corridorkey
${watchtower}

volumes:
  ck-weights:
  ck-weights-gvm:
  ck-weights-vm:
  ck-compile-cache:`;
		} else {
			return `services:
  corridorkey-node:
    image: ${nodeImage}
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
      - CK_MAIN_URL=${setupInfo.main_url}
      - CK_AUTH_TOKEN=${token}
      - CK_NODE_NAME=${name}
      - CK_NODE_GPUS=auto
    volumes:
      - ck-weights:/app/CorridorKeyModule/checkpoints
      - ck-weights-gvm:/app/gvm_core/weights
      - ck-weights-vm:/app/VideoMaMaInferenceModule/checkpoints
      - ck-compile-cache:/app/.cache/corridorkey
${watchtower}

volumes:
  ck-weights:
  ck-weights-gvm:
  ck-weights-vm:
  ck-compile-cache:`;
		}
	});

	let orgMap = $derived(Object.fromEntries(userOrgs.map(o => [o.org_id, o.name])));
	let activeTokens = $derived(nodeTokens.filter(t => !t.revoked).sort((a, b) => (orgMap[a.org_id] || '').localeCompare(orgMap[b.org_id] || '')));
	let revokedTokens = $derived(nodeTokens.filter(t => t.revoked));
</script>

<svelte:head>
	<title>Add Node — CorridorKey</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<a href="/nodes" class="back-link mono">&larr; Back to Fleet</a>
		<h1 class="page-title">Add a Node</h1>
		<p class="page-subtitle">Connect a GPU to the community render farm</p>
	</header>

	<!-- Step indicator -->
	<div class="steps">
		{#each [{ n: 1, label: 'GPU Type' }, { n: 2, label: 'Auth Token' }, { n: 3, label: 'Docker Compose' }, { n: 4, label: 'Run' }] as s}
			<button class="step" class:active={step === s.n} class:done={step > s.n} onclick={() => step = s.n}>
				<span class="step-num mono">{s.n}</span>
				<span class="step-label mono">{s.label}</span>
			</button>
		{/each}
	</div>

	<!-- Step content -->
	<div class="step-content">
		{#if step === 1}
			<h2 class="step-title">Select your GPU type</h2>
			<div class="gpu-select">
				<button class="gpu-option" class:selected={gpuVendor === 'nvidia'} onclick={() => gpuVendor = 'nvidia'}>
					<span class="gpu-brand">NVIDIA</span>
					<span class="gpu-detail mono">RTX 3060+ (12GB VRAM minimum)</span>
				</button>
				<button class="gpu-option" class:selected={gpuVendor === 'amd'} onclick={() => gpuVendor = 'amd'}>
					<span class="gpu-brand">AMD</span>
					<span class="gpu-detail mono">RX 7800 XT+ (ROCm, Windows .exe or Linux Docker)</span>
				</button>
			</div>

			<h3 class="subsection-title mono">STANDALONE BINARY (Windows)</h3>
			<div class="download-row">
				<a
					href={gpuVendor === 'nvidia'
						? 'https://huggingface.co/JamesNyeVRGuy/corridorkey-node/resolve/main/latest/corridorkey-node-nvidia-setup.exe'
						: 'https://huggingface.co/JamesNyeVRGuy/corridorkey-node/resolve/main/latest/corridorkey-node-amd-setup.exe'}
					target="_blank"
					rel="noopener"
					class="download-btn mono"
				>
					<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 1v9M4 7l4 4 4-4M2 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
					{gpuVendor === 'nvidia' ? 'NVIDIA' : 'AMD'} INSTALLER (.exe)
				</a>
				<a
					href={gpuVendor === 'nvidia'
						? 'https://huggingface.co/JamesNyeVRGuy/corridorkey-node/resolve/main/latest/corridorkey-node-nvidia-win-x64.zip'
						: 'https://huggingface.co/JamesNyeVRGuy/corridorkey-node/resolve/main/latest/corridorkey-node-amd-win-x64.zip'}
					target="_blank"
					rel="noopener"
					class="download-btn download-secondary mono"
				>
					Portable .zip
				</a>
			</div>
			<p class="step-hint mono">
				{#if gpuVendor === 'amd'}
					AMD binary ships with the HIP runtime bundled — no separate ROCm install required.
					Linux AMD users should use Docker (below).
				{:else}
					Linux NVIDIA users should use Docker (below) for easier GPU passthrough.
				{/if}
			</p>
			<button class="btn-primary mono" onclick={() => step = 2}>Next: Generate Token &rarr;</button>

		{:else if step === 2}
			<h2 class="step-title">Generate an auth token</h2>
			<div class="token-form">
				<label class="field">
					<span class="field-label mono">ORGANIZATION</span>
					<select class="select mono" bind:value={selectedOrgId}>
						{#each userOrgs as org}<option value={org.org_id}>{org.name}</option>{/each}
					</select>
				</label>
				<label class="field">
					<span class="field-label mono">NODE LABEL</span>
					<input type="text" class="input mono" bind:value={tokenLabel} placeholder="e.g. My RTX 3090" />
				</label>
				<button class="btn-primary mono" onclick={generateToken} disabled={tokenGenerating || !tokenLabel.trim()}>
					{tokenGenerating ? 'Generating...' : 'Generate Token'}
				</button>
			</div>

			{#if generatedToken}
				<div class="token-result">
					<span class="field-label mono">YOUR TOKEN (copy this — it won't be shown again)</span>
					<div class="token-display">
						<input type="text" readonly value={generatedToken} class="input mono token-input" />
						<button class="btn-copy mono" onclick={() => copyToClipboard(generatedToken)}>COPY</button>
					</div>
				</div>
			{/if}

		{:else if step === 3}
			<h2 class="step-title">Copy the Docker Compose file</h2>
			<div class="compose-wrap">
				<div class="compose-header">
					<span class="compose-label mono">docker-compose.yml</span>
					<button class="btn-copy mono" onclick={() => copyToClipboard(composeText)}>COPY</button>
				</div>
				<pre class="compose-code mono">{composeText}</pre>
			</div>
			<button class="btn-primary mono" onclick={() => step = 4}>Next: Run &rarr;</button>

		{:else if step === 4}
			<h2 class="step-title">Start the node</h2>
			<div class="run-instructions">
				<div class="run-step">
					<span class="run-num mono">1</span>
					<div>
						<p>Save the compose file as <code class="mono">docker-compose.yml</code></p>
					</div>
				</div>
				<div class="run-step">
					<span class="run-num mono">2</span>
					<div>
						<p>Run:</p>
						<pre class="run-cmd mono">docker compose up -d</pre>
					</div>
				</div>
				<div class="run-step">
					<span class="run-num mono">3</span>
					<div>
						<p>The node will appear on the <a href="/nodes">fleet page</a> within 30 seconds</p>
					</div>
				</div>
			</div>

			<!-- CRKY-193: distro-specific setup notes for users whose Docker install
			     doesn't come with the GPU runtime pre-configured. -->
			{#if gpuVendor === 'nvidia'}
			<details class="distro-notes">
				<summary class="distro-summary mono">Getting "could not select device driver 'nvidia'" or similar?</summary>
				<div class="distro-body">
					<p class="distro-intro">
						Your Docker install needs the NVIDIA Container Toolkit and the CDI runtime
						configured. Below are the exact commands per distro. After running them,
						restart the container with <code class="mono">docker compose up -d --force-recreate</code>.
					</p>

					<h4 class="distro-title mono">Ubuntu / Debian</h4>
					<pre class="run-cmd mono">curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker</pre>

					<h4 class="distro-title mono">Arch Linux / Manjaro</h4>
					<pre class="run-cmd mono">sudo pacman -S nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# If you still get "unresolvable CDI devices nvidia.com/gpu=all":
sudo mkdir -p /etc/cdi
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
nvidia-ctk cdi list    # should show nvidia.com/gpu=all
sudo systemctl restart docker</pre>

					<h4 class="distro-title mono">Fedora / RHEL / CentOS</h4>
					<pre class="run-cmd mono">curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
  | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo
sudo dnf install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker</pre>

					<h4 class="distro-title mono">Verify it works</h4>
					<pre class="run-cmd mono">docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi</pre>
					<p class="distro-note">
						If that prints your GPU, the runtime is wired up and the CorridorKey node
						will work. If it errors, the problem is your Docker/driver install, not the
						node image.
					</p>
				</div>
			</details>
			{:else}
			<details class="distro-notes">
				<summary class="distro-summary mono">Getting "permission denied: /dev/kfd" or no GPU detected?</summary>
				<div class="distro-body">
					<p class="distro-intro">
						AMD GPUs need ROCm drivers on the host (the container does not ship them) and
						the user running Docker must be in the <code class="mono">video</code> and
						<code class="mono">render</code> groups so the container can access
						<code class="mono">/dev/kfd</code> and <code class="mono">/dev/dri</code>.
					</p>

					<h4 class="distro-title mono">Ubuntu 22.04 / 24.04</h4>
					<pre class="run-cmd mono">sudo apt-get update
sudo apt-get install -y "linux-headers-$(uname -r)" "linux-modules-extra-$(uname -r)"
wget https://repo.radeon.com/amdgpu-install/6.3/ubuntu/jammy/amdgpu-install_6.3.60300-1_all.deb
sudo apt-get install -y ./amdgpu-install_6.3.60300-1_all.deb
sudo amdgpu-install --usecase=dkms,rocm
sudo usermod -aG video,render $USER
# Log out and back in for group membership to apply, then verify:
rocminfo | head</pre>

					<h4 class="distro-title mono">Arch Linux / Manjaro</h4>
					<pre class="run-cmd mono">sudo pacman -S rocm-hip-runtime rocm-hip-libraries rocm-smi-lib
sudo usermod -aG video,render $USER
# Log out and back in, then verify:
rocminfo | head</pre>

					<h4 class="distro-title mono">Fedora / RHEL</h4>
					<pre class="run-cmd mono">sudo dnf install -y rocm-hip rocm-runtime rocm-smi
sudo usermod -aG video,render $USER
# Log out and back in, then verify:
rocminfo | head</pre>

					<h4 class="distro-title mono">Verify it works</h4>
					<pre class="run-cmd mono">docker run --rm --device=/dev/kfd --device=/dev/dri --group-add video \
  rocm/dev-ubuntu-22.04 rocminfo</pre>
					<p class="distro-note">
						If that prints your GPU agents, the runtime is wired up and the CorridorKey
						AMD node will work. If you see "permission denied" on <code class="mono">/dev/kfd</code>,
						you forgot to log out / back in after <code class="mono">usermod</code>.
						Windows AMD users should use the standalone <code class="mono">.exe</code> above
						(HIP runtime is bundled).
					</p>
				</div>
			</details>
			{/if}
		{/if}
	</div>

	<!-- Token management (always visible) -->
	{#if activeTokens.length > 0 || revokedTokens.length > 0}
		<div class="token-section">
			<h3 class="section-title mono">YOUR TOKENS</h3>
			{#if activeTokens.length > 0}
				<div class="token-list">
					{#each activeTokens as t}
						<div class="token-row">
							<span class="token-preview mono">{t.token_preview}</span>
							<span class="token-name">{t.label}</span>
							<span class="token-org mono">{orgMap[t.org_id] || 'Unknown'}</span>
							{#if t.node_id}
								<span class="token-status connected mono">CONNECTED</span>
							{:else}
								<span class="token-status unused mono">UNUSED</span>
							{/if}
							<button class="btn-revoke mono" onclick={() => revokeToken(t.token_preview)}>REVOKE</button>
						</div>
					{/each}
				</div>
			{/if}
			{#if revokedTokens.length > 0}
				<button class="revoked-toggle mono" onclick={() => showRevokedTokens = !showRevokedTokens}>
					REVOKED ({revokedTokens.length}) {showRevokedTokens ? '▲' : '▼'}
				</button>
				{#if showRevokedTokens}
					<div class="token-list">
						{#each revokedTokens as t}
							<div class="token-row revoked">
								<span class="token-preview mono">{t.token_preview}</span>
								<span class="token-name">{t.label}</span>
								<span class="token-status revoked-badge mono">REVOKED</span>
							</div>
						{/each}
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</div>

<style>
	.page { padding: var(--sp-5) var(--sp-6); display: flex; flex-direction: column; gap: var(--sp-4); max-width: 700px; }

	.page-header { display: flex; flex-direction: column; gap: var(--sp-1); }
	.back-link { font-size: 11px; color: var(--text-tertiary); text-decoration: none; transition: color 0.15s; }
	.back-link:hover { color: var(--accent); }
	.page-title { font-family: var(--font-sans); font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }
	.page-subtitle { font-size: 14px; color: var(--text-secondary); }

	/* Steps indicator */
	.steps { display: flex; gap: 0; border-bottom: 1px solid var(--border); }
	.step {
		display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3);
		border: none; background: none; color: var(--text-tertiary); cursor: pointer; transition: all 0.15s;
		border-bottom: 2px solid transparent;
	}
	.step:hover { color: var(--text-secondary); }
	.step.active { color: var(--accent); border-bottom-color: var(--accent); }
	.step.done { color: var(--state-complete); }
	.step-num { font-size: 10px; font-weight: 700; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; border-radius: 50%; border: 1px solid currentColor; }
	.step.active .step-num { background: var(--accent); color: #000; border-color: var(--accent); }
	.step.done .step-num { background: var(--state-complete); color: #000; border-color: var(--state-complete); }
	.step-label { font-size: 11px; letter-spacing: 0.04em; }

	/* Step content */
	.step-content { display: flex; flex-direction: column; gap: var(--sp-3); }
	.step-title { font-family: var(--font-sans); font-size: 16px; font-weight: 600; }
	.subsection-title { font-size: 10px; letter-spacing: 0.1em; color: var(--text-tertiary); margin-top: var(--sp-2); }
	.step-hint { font-size: 11px; color: var(--text-tertiary); }

	.btn-primary {
		padding: 8px 16px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em;
		background: var(--accent); color: #000; border: none; border-radius: var(--radius-sm);
		cursor: pointer; transition: all 0.15s; align-self: flex-start;
	}
	.btn-primary:hover { background: #fff; }
	.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }

	/* GPU select */
	.gpu-select { display: flex; gap: var(--sp-3); }
	.gpu-option {
		flex: 1; padding: var(--sp-4); background: var(--surface-2); border: 2px solid var(--border);
		border-radius: var(--radius-md); cursor: pointer; transition: all 0.15s;
		display: flex; flex-direction: column; gap: var(--sp-1); text-align: left; font: inherit; color: inherit;
	}
	.gpu-option:hover { border-color: var(--text-tertiary); }
	.gpu-option.selected { border-color: var(--accent); background: rgba(255, 242, 3, 0.04); }
	.gpu-brand { font-size: 16px; font-weight: 700; color: var(--text-primary); }
	.gpu-detail { font-size: 11px; color: var(--text-tertiary); }

	/* Download buttons */
	.download-row { display: flex; gap: var(--sp-2); }
	.download-btn {
		display: inline-flex; align-items: center; gap: 6px; text-decoration: none;
		font-size: 11px; letter-spacing: 0.06em; font-weight: 600;
		padding: 8px 16px; background: var(--accent); color: #000;
		border-radius: var(--radius-sm); transition: all 0.15s;
	}
	.download-btn:hover { background: #fff; }
	.download-secondary {
		background: var(--surface-3); color: var(--text-secondary);
		border: 1px solid var(--border);
	}
	.download-secondary:hover { background: var(--surface-2); color: var(--text-primary); }

	/* Token form */
	.token-form { display: flex; flex-direction: column; gap: var(--sp-3); }
	.field { display: flex; flex-direction: column; gap: 4px; }
	.field-label { font-size: 9px; letter-spacing: 0.08em; color: var(--text-tertiary); }
	.input, .select {
		padding: 8px 10px; background: var(--surface-3); border: 1px solid var(--border);
		border-radius: 6px; color: var(--text-primary); font-size: 13px; outline: none; width: 100%;
	}
	.input:focus { border-color: var(--accent); }
	.input::placeholder { color: var(--text-tertiary); }

	.token-result { display: flex; flex-direction: column; gap: var(--sp-1); margin-top: var(--sp-2); }
	.token-display { display: flex; gap: var(--sp-1); }
	.token-input { flex: 1; }
	.btn-copy {
		padding: 6px 12px; font-size: 10px; letter-spacing: 0.06em; font-weight: 600;
		background: var(--accent); color: #000; border: none; border-radius: var(--radius-sm); cursor: pointer;
	}
	.btn-copy:hover { background: #fff; }

	/* Compose */
	.compose-wrap { border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; }
	.compose-header {
		display: flex; justify-content: space-between; align-items: center;
		padding: var(--sp-2) var(--sp-3); background: var(--surface-3); border-bottom: 1px solid var(--border);
	}
	.compose-label { font-size: 10px; color: var(--text-tertiary); letter-spacing: 0.06em; }
	.compose-code {
		padding: var(--sp-3); font-size: 11px; line-height: 1.5; color: var(--text-secondary);
		background: var(--surface-1); overflow-x: auto; white-space: pre;
	}

	/* Run instructions */
	.run-instructions { display: flex; flex-direction: column; gap: var(--sp-3); }
	.run-step { display: flex; gap: var(--sp-3); align-items: flex-start; }
	.run-num {
		width: 24px; height: 24px; display: flex; align-items: center; justify-content: center;
		border-radius: 50%; background: var(--surface-3); color: var(--text-secondary); font-size: 11px;
		flex-shrink: 0; font-weight: 600;
	}
	.run-step p { font-size: 13px; color: var(--text-secondary); }
	.run-step a { color: var(--accent); }
	.run-cmd {
		font-size: 12px; background: var(--surface-2); border: 1px solid var(--border);
		border-radius: var(--radius-sm); padding: var(--sp-2) var(--sp-3); color: var(--text-primary);
		margin-top: var(--sp-1);
	}

	/* Token management */
	.token-section {
		border-top: 1px solid var(--border); padding-top: var(--sp-4);
		display: flex; flex-direction: column; gap: var(--sp-2);
	}
	.section-title { font-size: 10px; letter-spacing: 0.1em; color: var(--text-tertiary); font-weight: 600; }
	.token-list {
		border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden;
		background: var(--surface-1);
	}
	.token-row {
		display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3);
		border-bottom: 1px solid var(--border-subtle); font-size: 12px;
	}
	.token-row:last-child { border-bottom: none; }
	.token-row.revoked { opacity: 0.5; }
	.token-preview { font-size: 11px; color: var(--text-tertiary); min-width: 80px; }
	.token-name { flex: 1; color: var(--text-primary); }
	.token-org { font-size: 10px; color: var(--secondary); }
	.token-status { font-size: 9px; letter-spacing: 0.06em; }
	.token-status.connected { color: var(--state-complete); }
	.token-status.unused { color: var(--text-tertiary); }
	.token-status.revoked-badge { color: var(--state-error); }
	.btn-revoke {
		font-size: 9px; letter-spacing: 0.06em; padding: 2px 6px; border-radius: 3px;
		background: transparent; border: 1px solid rgba(255, 82, 82, 0.3); color: var(--state-error);
		cursor: pointer; transition: all 0.15s;
	}
	.btn-revoke:hover { background: rgba(255, 82, 82, 0.1); }
	.revoked-toggle {
		font-size: 10px; color: var(--text-tertiary); background: none; border: none; cursor: pointer;
		padding: var(--sp-1) 0; transition: color 0.15s;
	}
	.revoked-toggle:hover { color: var(--text-secondary); }

	.distro-notes {
		margin-top: var(--sp-5); background: var(--surface-2); border: 1px solid var(--border);
		border-radius: var(--radius-md); padding: var(--sp-3);
	}
	.distro-summary {
		font-size: 11px; color: var(--text-secondary); cursor: pointer; font-weight: 600;
		letter-spacing: 0.04em; padding: var(--sp-1) 0;
	}
	.distro-summary:hover { color: var(--text-primary); }
	.distro-body { padding-top: var(--sp-2); display: flex; flex-direction: column; gap: var(--sp-2); }
	.distro-intro, .distro-note { font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
	.distro-title {
		font-size: 10px; color: var(--text-tertiary); letter-spacing: 0.08em;
		margin-top: var(--sp-2); text-transform: uppercase;
	}
</style>
