<script lang="ts">
	const STORAGE_KEY = 'ck:welcome_dismissed';

	let visible = $state(false);
	let step = $state(0);

	$effect(() => {
		if (typeof window !== 'undefined') {
			visible = !localStorage.getItem(STORAGE_KEY);
		}
	});

	function dismiss() {
		localStorage.setItem(STORAGE_KEY, '1');
		visible = false;
	}

	function next() {
		if (step < steps.length - 1) step++;
		else dismiss();
	}

	function back() {
		if (step > 0) step--;
	}

	export function show() {
		step = 0;
		visible = true;
	}

	const steps = [
		{
			title: 'UPLOAD FOOTAGE',
			desc: 'Drag & drop your green screen video or a ZIP of image frames onto the Clips page. Videos are automatically extracted into individual frames for processing.',
			hint: 'Supports MP4, MOV, MKV, AVI, and most image formats (PNG, EXR, JPG).',
			icon: 'M4 14l8-8 8 8M12 6v14M4 22h16',
		},
		{
			title: 'ALPHA GENERATION',
			desc: 'Click "Run Pipeline" on your clip. GVM (Generative Video Matting) analyzes your footage using a temporal-aware diffusion model and generates alpha matte hints automatically.',
			hint: 'No manual rotoscoping needed. GVM understands motion across frames.',
			icon: 'M2 12h4l3-7 4 14 3-7h4',
		},
		{
			title: 'NEURAL KEYING',
			desc: 'CorridorKey\'s neural network takes the RGB frames and alpha hints, then produces a clean foreground, refined matte, and compositing-ready premultiplied EXR output.',
			hint: 'Output passes: FG (foreground), Matte (alpha), Comp (preview), Processed (premultiplied EXR).',
			icon: 'M12 2L2 7v10l10 5 10-5V7L12 2zM2 7l10 5M12 12l10-5M12 12v10',
		},
		{
			title: 'DOWNLOAD RESULTS',
			desc: 'Preview your results frame-by-frame with the built-in viewer. Compare input vs. output with the wipe tool. Download individual passes or the full EXR sequence.',
			hint: 'Ready for Nuke, After Effects, Fusion, or any compositing application.',
			icon: 'M12 4v12M8 12l4 4 4-4M4 18h16',
		},
		{
			title: 'RENDER FARM',
			desc: 'Connect additional machines as worker nodes to distribute processing. Jobs are automatically split and dispatched across all available GPUs on the network.',
			hint: 'Go to the Nodes page to generate a token and set up your first node.',
			icon: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6zM7 10v4M17 10v4M10 7h4M10 17h4',
		},
	];

	let current = $derived(steps[step]);
	let isLast = $derived(step === steps.length - 1);
</script>

{#if visible}
<div class="welcome-overlay" role="dialog" aria-label="Welcome to CorridorKey">
	<div class="welcome-panel">
		<!-- Header -->
		<div class="panel-header">
			<div class="header-row">
				<span class="header-tag mono">BRIEFING</span>
				<span class="header-step mono">{step + 1} / {steps.length}</span>
			</div>
			<h1 class="header-title mono">CORRIDORKEY</h1>
			<div class="header-rule"></div>
		</div>

		<!-- Step content -->
		{#key step}
		<div class="step-content">
			<div class="step-icon-wrap">
				<div class="step-num mono">{String(step + 1).padStart(2, '0')}</div>
				<div class="step-icon">
					<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
						<path d={current.icon} />
					</svg>
				</div>
			</div>
			<h2 class="step-title mono">{current.title}</h2>
			<p class="step-desc">{current.desc}</p>
			<p class="step-hint mono">{current.hint}</p>
		</div>
		{/key}

		<!-- Progress dots -->
		<div class="progress-row">
			{#each steps as _, i}
				<button
					class="progress-dot"
					class:active={i === step}
					class:done={i < step}
					onclick={() => step = i}
					aria-label="Go to step {i + 1}"
				></button>
			{/each}
		</div>

		<!-- Navigation -->
		<div class="panel-footer">
			<button class="skip-btn mono" onclick={dismiss}>SKIP</button>
			<div class="nav-btns">
				{#if step > 0}
					<button class="back-btn mono" onclick={back}>
						<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><path d="M11 7H3M6 4L3 7l3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
						BACK
					</button>
				{/if}
				<button class="next-btn mono" onclick={next}>
					{isLast ? 'GET STARTED' : 'NEXT'}
					<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><path d="M3 7h8M8 4l3 3-3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
				</button>
			</div>
		</div>
	</div>
</div>
{/if}

<style>
	.welcome-overlay {
		position: fixed;
		inset: 0;
		z-index: 9000;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.85);
		backdrop-filter: blur(8px);
		animation: fade-in 0.3s ease;
	}

	@keyframes fade-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	.welcome-panel {
		width: 100%;
		max-width: 480px;
		margin: var(--sp-4);
		background: var(--surface-1);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		position: relative;
		overflow: hidden;
	}

	/* Accent corner markers */
	.welcome-panel::before,
	.welcome-panel::after {
		content: '';
		position: absolute;
		width: 16px;
		height: 16px;
		border-color: var(--accent);
		border-style: solid;
		opacity: 0.4;
		z-index: 1;
	}
	.welcome-panel::before {
		top: -1px;
		left: -1px;
		border-width: 2px 0 0 2px;
		border-radius: var(--radius-lg) 0 0 0;
	}
	.welcome-panel::after {
		bottom: -1px;
		right: -1px;
		border-width: 0 2px 2px 0;
		border-radius: 0 0 var(--radius-lg) 0;
	}

	/* Header */
	.panel-header {
		padding: var(--sp-5) var(--sp-5) 0;
	}

	.header-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: var(--sp-2);
	}

	.header-tag {
		font-size: 9px;
		letter-spacing: 0.3em;
		color: var(--accent);
		opacity: 0.7;
	}

	.header-step {
		font-size: 10px;
		letter-spacing: 0.08em;
		color: var(--text-tertiary);
	}

	.header-title {
		font-size: 18px;
		font-weight: 600;
		letter-spacing: 0.15em;
		color: var(--text-primary);
	}

	.header-rule {
		height: 1px;
		background: linear-gradient(90deg, var(--accent), var(--border), transparent);
		margin-top: var(--sp-3);
		opacity: 0.5;
	}

	/* Step content */
	.step-content {
		padding: var(--sp-5);
		min-height: 240px;
		display: flex;
		flex-direction: column;
		animation: step-slide 0.25s ease;
	}

	@keyframes step-slide {
		from {
			opacity: 0;
			transform: translateX(12px);
		}
		to {
			opacity: 1;
			transform: translateX(0);
		}
	}

	.step-icon-wrap {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
		margin-bottom: var(--sp-4);
	}

	.step-num {
		font-size: 28px;
		font-weight: 700;
		color: var(--accent);
		opacity: 0.25;
		line-height: 1;
	}

	.step-icon {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--accent);
		background: var(--accent-muted);
		border-radius: var(--radius-md);
	}

	.step-title {
		font-size: 14px;
		font-weight: 600;
		letter-spacing: 0.12em;
		color: var(--text-primary);
		margin-bottom: var(--sp-3);
	}

	.step-desc {
		font-size: 14px;
		color: var(--text-secondary);
		line-height: 1.55;
		font-weight: 300;
		margin-bottom: var(--sp-3);
		flex: 1;
	}

	.step-hint {
		font-size: 11px;
		color: var(--text-tertiary);
		letter-spacing: 0.02em;
		padding: var(--sp-2) var(--sp-3);
		background: var(--surface-2);
		border-left: 2px solid var(--accent-muted);
		border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
		line-height: 1.4;
	}

	/* Progress dots */
	.progress-row {
		display: flex;
		justify-content: center;
		gap: 8px;
		padding: 0 var(--sp-5) var(--sp-3);
	}

	.progress-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		border: 1px solid var(--text-tertiary);
		background: transparent;
		cursor: pointer;
		padding: 0;
		transition: all 0.2s;
	}

	.progress-dot.active {
		background: var(--accent);
		border-color: var(--accent);
		transform: scale(1.2);
	}

	.progress-dot.done {
		background: var(--text-tertiary);
		border-color: var(--text-tertiary);
	}

	/* Footer */
	.panel-footer {
		padding: var(--sp-3) var(--sp-5) var(--sp-4);
		display: flex;
		align-items: center;
		justify-content: space-between;
		border-top: 1px solid var(--border-subtle);
	}

	.nav-btns {
		display: flex;
		gap: var(--sp-2);
	}

	.skip-btn {
		font-size: 10px;
		letter-spacing: 0.08em;
		color: var(--text-tertiary);
		background: none;
		border: none;
		cursor: pointer;
		padding: 6px 10px;
		border-radius: var(--radius-sm);
		transition: color 0.15s;
	}
	.skip-btn:hover { color: var(--text-secondary); }

	.back-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 11px;
		letter-spacing: 0.06em;
		color: var(--text-secondary);
		background: none;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		cursor: pointer;
		padding: 8px 14px;
		transition: all 0.15s;
	}
	.back-btn:hover {
		border-color: var(--text-tertiary);
		color: var(--text-primary);
	}

	.next-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 18px;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.1em;
		background: var(--accent);
		color: var(--surface-0);
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition: all 0.15s;
	}
	.next-btn:hover {
		background: #fff;
		color: #000;
	}
</style>
