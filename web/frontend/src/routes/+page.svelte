<script lang="ts">
	import { onMount } from 'svelte';

	let ready = $state(false);
	let loggedIn = $state(false);

	onMount(() => {
		loggedIn = !!localStorage.getItem('ck:auth_token');
		ready = true;
	});
</script>

<svelte:head>
	<title>CorridorKey Cloud — AI Green Screen Keying</title>
</svelte:head>

{#if ready}
<div class="landing">
	<!-- Nav -->
	{#if !loggedIn}
	<nav class="landing-nav">
		<a href="/" class="nav-logo">
			<img src="/Corridor_Digital_Logo.svg" alt="Corridor Digital" class="nav-logo-img" />
			<span class="nav-product mono">CORRIDORKEY</span>
			<span class="beta-badge mono">BETA</span>
		</a>
		<div class="nav-actions">
			<a href="https://discord.gg/44tHTSCGVQ" target="_blank" rel="noopener" class="nav-link">Discord</a>
			<a href="/login" class="nav-link">Log In</a>
			<a href="/signup" class="nav-btn">Get Started</a>
		</div>
	</nav>
	{/if}

	<!-- Hero -->
	<section class="hero">
		<div class="hero-badge mono">OPEN SOURCE + CLOUD</div>
		<h1 class="hero-title">AI Green Screen Keying<br /><span class="hero-accent">No GPU Required</span></h1>
		<p class="hero-sub">
			Upload green screen footage. Get production-ready alpha mattes, clean foreground plates,
			and compositing-ready EXRs — processed on a community-powered GPU farm.
		</p>
		<div class="hero-actions">
			<a href="/signup" class="btn-primary">Start Keying</a>
			<a href="https://github.com/JamesNyeVRGuy/CorridorKey" class="btn-outline" target="_blank" rel="noopener">View on GitHub</a>
		</div>
	</section>

	<!-- How it works -->
	<section class="section">
		<h2 class="section-label mono">HOW IT WORKS</h2>
		<div class="steps">
			<div class="step">
				<div class="step-num mono">01</div>
				<h3 class="step-title">Upload</h3>
				<p class="step-desc">Drag in your green screen video, image sequence, or single frame. Any resolution, any length.</p>
			</div>
			<div class="step-arrow">→</div>
			<div class="step">
				<div class="step-num mono">02</div>
				<h3 class="step-title">Process</h3>
				<p class="step-desc">CorridorKey's neural network generates alpha mattes and clean foreground plates. Sharded across multiple GPUs for speed.</p>
			</div>
			<div class="step-arrow">→</div>
			<div class="step">
				<div class="step-num mono">03</div>
				<h3 class="step-title">Download</h3>
				<p class="step-desc">Get your keyed EXRs — premultiplied RGBA ready to drop into Nuke, After Effects, DaVinci, or Blender.</p>
			</div>
		</div>
	</section>

	<!-- GPU Farm -->
	<section class="section farm-section">
		<div class="farm-content">
			<h2 class="section-label mono">COMMUNITY GPU FARM</h2>
			<h3 class="farm-title">Contribute a GPU.<br />Earn processing credits.</h3>
			<p class="farm-desc">
				CorridorKey Cloud runs on a distributed render farm powered by the community.
				Connect your idle GPU as a node — it processes jobs for other users, and you earn
				credits to process your own footage. One Docker command to set up.
			</p>
			<div class="farm-stats">
				<div class="farm-stat">
					<span class="farm-stat-value mono" id="stat-nodes">—</span>
					<span class="farm-stat-label">Nodes Online</span>
				</div>
				<div class="farm-stat">
					<span class="farm-stat-value mono" id="stat-gpus">—</span>
					<span class="farm-stat-label">Total GPUs</span>
				</div>
				<div class="farm-stat">
					<span class="farm-stat-value mono" id="stat-frames">—</span>
					<span class="farm-stat-label">Frames Processed</span>
				</div>
			</div>
		</div>
	</section>

	{#if !loggedIn}
	<!-- CTA -->
	<section class="section cta-section">
		<h2 class="cta-title">Ready to key?</h2>
		<p class="cta-desc">Free to start. No credit card. No GPU needed.</p>
		<a href="/signup" class="btn-primary btn-lg">Get Started</a>
	</section>

	<!-- Footer -->
	<footer class="landing-footer">
		<div class="footer-links">
			<a href="/terms">Terms</a>
			<a href="/privacy">Privacy</a>
			<a href="https://github.com/JamesNyeVRGuy/CorridorKey" target="_blank" rel="noopener">GitHub</a>
			<a href="https://discord.gg/44tHTSCGVQ" target="_blank" rel="noopener">Discord</a>
			<a href="/status">Status</a>
		</div>
		<span class="footer-copy mono">CorridorKey — Corridor Digital</span>
	</footer>
	{/if}
</div>

<script>
	// Fetch live stats for the farm section
	fetch('/api/status').then(r => r.json()).then(data => {
		if (data.nodes_online !== undefined) {
			const el = document.getElementById('stat-nodes');
			if (el) el.textContent = String(data.nodes_online ?? 0);
		}
		if (data.total_gpus !== undefined) {
			const el = document.getElementById('stat-gpus');
			if (el) el.textContent = String(data.total_gpus ?? 0);
		}
		if (data.frames_processed !== undefined) {
			const el = document.getElementById('stat-frames');
			if (el) el.textContent = Number(data.frames_processed ?? 0).toLocaleString();
		}
	}).catch(() => {});
</script>
{/if}

<style>
	/* Override body overflow:hidden from app.css — the app shell
	   handles its own scrolling, but the landing page is a normal
	   scrollable document. */
	:global(body:has(.landing)) {
		overflow: auto;
	}

	.landing {
		min-height: 100vh;
		background: var(--surface-0);
	}

	/* Nav */
	.landing-nav {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: var(--sp-4) var(--sp-6);
		max-width: 1200px;
		margin: 0 auto;
	}
	.nav-logo {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
	}
	.nav-logo-img { height: 28px; width: auto; }
	.nav-product {
		font-size: 9px;
		letter-spacing: 0.2em;
		color: var(--text-tertiary);
	}
	.beta-badge {
		font-size: 9px;
		letter-spacing: 0.1em;
		padding: 2px 6px;
		border-radius: 4px;
		background: rgba(255, 242, 3, 0.12);
		color: var(--accent);
		border: 1px solid rgba(255, 242, 3, 0.2);
	}
	.nav-actions {
		display: flex;
		align-items: center;
		gap: var(--sp-4);
	}
	.nav-link {
		font-size: 14px;
		color: var(--text-secondary);
		transition: color 0.15s;
	}
	.nav-link:hover { color: var(--text-primary); }
	.nav-btn {
		font-size: 13px;
		font-weight: 600;
		padding: 8px 20px;
		background: var(--accent);
		color: #000;
		border-radius: var(--radius-md);
		transition: all 0.15s;
	}
	.nav-btn:hover {
		background: #fff;
		box-shadow: 0 0 20px rgba(255, 242, 3, 0.3);
	}

	/* Hero */
	.hero {
		text-align: center;
		padding: 80px var(--sp-6) 60px;
		max-width: 800px;
		margin: 0 auto;
	}
	.hero-badge {
		display: inline-block;
		font-size: 10px;
		letter-spacing: 0.15em;
		color: var(--accent);
		border: 1px solid var(--border-active);
		border-radius: 20px;
		padding: 4px 14px;
		margin-bottom: var(--sp-5);
	}
	.hero-title {
		font-family: var(--font-sans);
		font-size: clamp(32px, 5vw, 56px);
		font-weight: 700;
		line-height: 1.1;
		color: var(--text-primary);
		margin-bottom: var(--sp-5);
	}
	.hero-accent { color: var(--accent); }
	.hero-sub {
		font-size: 17px;
		line-height: 1.6;
		color: var(--text-secondary);
		max-width: 600px;
		margin: 0 auto var(--sp-8);
	}
	.hero-actions {
		display: flex;
		gap: var(--sp-4);
		justify-content: center;
		flex-wrap: wrap;
	}

	/* Buttons */
	.btn-primary {
		display: inline-block;
		font-size: 15px;
		font-weight: 600;
		padding: 12px 32px;
		background: var(--accent);
		color: #000;
		border-radius: var(--radius-md);
		transition: all 0.2s;
	}
	.btn-primary:hover {
		background: #fff;
		box-shadow: 0 0 30px rgba(255, 242, 3, 0.25);
		transform: translateY(-1px);
	}
	.btn-primary.btn-lg {
		font-size: 17px;
		padding: 14px 40px;
	}
	.btn-outline {
		display: inline-block;
		font-size: 15px;
		font-weight: 500;
		padding: 12px 32px;
		border: 1px solid var(--border);
		color: var(--text-secondary);
		border-radius: var(--radius-md);
		transition: all 0.2s;
	}
	.btn-outline:hover {
		border-color: var(--text-tertiary);
		color: var(--text-primary);
	}

	/* Sections */
	.section {
		max-width: 1000px;
		margin: 0 auto;
		padding: 60px var(--sp-6);
	}
	.section-label {
		font-size: 10px;
		letter-spacing: 0.2em;
		color: var(--accent);
		margin-bottom: var(--sp-6);
		text-align: center;
	}

	/* Steps */
	.steps {
		display: flex;
		align-items: flex-start;
		justify-content: center;
		gap: var(--sp-4);
		flex-wrap: wrap;
	}
	.step {
		flex: 1;
		min-width: 200px;
		max-width: 280px;
		padding: var(--sp-5);
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
	}
	.step-num {
		font-size: 28px;
		font-weight: 700;
		color: var(--accent);
		margin-bottom: var(--sp-2);
	}
	.step-title {
		font-size: 18px;
		font-weight: 600;
		color: var(--text-primary);
		margin-bottom: var(--sp-2);
	}
	.step-desc {
		font-size: 13px;
		line-height: 1.6;
		color: var(--text-secondary);
	}
	.step-arrow {
		font-size: 24px;
		color: var(--text-tertiary);
		padding-top: 50px;
	}

	/* Farm */
	.farm-section {
		background: var(--surface-1);
		max-width: none;
		padding: 80px var(--sp-6);
	}
	.farm-content {
		max-width: 800px;
		margin: 0 auto;
		text-align: center;
	}
	.farm-title {
		font-size: clamp(24px, 4vw, 38px);
		font-weight: 700;
		color: var(--text-primary);
		line-height: 1.2;
		margin-bottom: var(--sp-4);
	}
	.farm-desc {
		font-size: 15px;
		line-height: 1.7;
		color: var(--text-secondary);
		margin-bottom: var(--sp-8);
	}
	.farm-stats {
		display: flex;
		justify-content: center;
		gap: var(--sp-10);
		flex-wrap: wrap;
	}
	.farm-stat { text-align: center; }
	.farm-stat-value {
		display: block;
		font-size: 32px;
		font-weight: 700;
		color: var(--accent);
		margin-bottom: var(--sp-1);
	}
	.farm-stat-label {
		font-size: 12px;
		color: var(--text-tertiary);
	}

	/* CTA */
	.cta-section {
		text-align: center;
		padding: 80px var(--sp-6);
	}
	.cta-title {
		font-size: clamp(24px, 4vw, 38px);
		font-weight: 700;
		color: var(--text-primary);
		margin-bottom: var(--sp-3);
	}
	.cta-desc {
		font-size: 15px;
		color: var(--text-secondary);
		margin-bottom: var(--sp-6);
	}

	/* Footer */
	.landing-footer {
		border-top: 1px solid var(--border);
		padding: var(--sp-5) var(--sp-6);
		display: flex;
		align-items: center;
		justify-content: space-between;
		max-width: 1200px;
		margin: 0 auto;
		flex-wrap: wrap;
		gap: var(--sp-3);
	}
	.footer-links {
		display: flex;
		gap: var(--sp-5);
	}
	.footer-links a {
		font-size: 13px;
		color: var(--text-tertiary);
		transition: color 0.15s;
	}
	.footer-links a:hover { color: var(--text-secondary); }
	.footer-copy {
		font-size: 10px;
		letter-spacing: 0.06em;
		color: var(--text-tertiary);
	}

	/* Mobile */
	@media (max-width: 640px) {
		.step-arrow { display: none; }
		.steps { flex-direction: column; align-items: center; }
		.farm-stats { gap: var(--sp-6); }
		.hero { padding: 50px var(--sp-4) 40px; }
	}
</style>
