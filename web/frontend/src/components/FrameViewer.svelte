<script lang="ts">
	import { api } from '$lib/api';
	import { onMount } from 'svelte';

	let {
		clipName,
		frameCount = 0,
		availablePasses = ['input'],
		completedFrames = -1,
	}: {
		clipName: string;
		frameCount?: number;
		availablePasses?: string[];
		completedFrames?: number;
	} = $props();

	const outputPasses = new Set(['fg', 'matte', 'comp', 'processed']);
	let isOutputPass = $derived(outputPasses.has(selectedPass));
	let frameNotReady = $derived(
		completedFrames >= 0 && isOutputPass && currentFrame >= completedFrames
	);
	let compareNotReady = $derived(
		completedFrames >= 0 && outputPasses.has(comparePass) && currentFrame >= completedFrames
	);
	let progressPct = $derived(
		completedFrames >= 0 && frameCount > 0 ? (completedFrames / frameCount) * 100 : -1
	);

	let currentFrame = $state(0);
	let selectedPass = $state('input');
	let loading = $state(false);
	let error = $state(false);
	let mode = $state<'frame' | 'video' | 'compare'>('frame');
	let compareMode = $state<'split' | 'wipe'>('split');
	let playbackFps = $state(24);
	let comparePass = $state('input');

	// Video encode progress
	let encodeStatus = $state<string>('idle'); // idle, encoding, stitching, ready, error
	let encodeCurrent = $state(0);
	let encodeTotal = $state(0);
	let encodePollTimer: ReturnType<typeof setInterval> | null = null;

	function startEncodePolling() {
		stopEncodePolling();
		encodeStatus = 'encoding';
		encodeCurrent = 0;
		encodeTotal = 0;
		encodePollTimer = setInterval(async () => {
			try {
				const res = await fetch(
					`/api/preview/${encodeURIComponent(clipName)}/${selectedPass}/video/progress?fps=${playbackFps}`
				);
				const data = await res.json();
				encodeStatus = data.status;
				encodeCurrent = data.current ?? 0;
				encodeTotal = data.total ?? 0;
				if (data.status === 'ready' || data.status === 'error' || data.status === 'idle') {
					stopEncodePolling();
				}
			} catch {
				// ignore
			}
		}, 500);
	}

	function stopEncodePolling() {
		if (encodePollTimer) {
			clearInterval(encodePollTimer);
			encodePollTimer = null;
		}
	}

	$effect(() => {
		if (mode === 'video') {
			startEncodePolling();
		} else {
			stopEncodePolling();
			encodeStatus = 'idle';
		}
	});

	// Wipe state
	let wipePos = $state(50); // percentage 0-100
	let wipeDragging = $state(false);
	let wipeViewport: HTMLDivElement | undefined = $state();

	function onWipePointerDown(e: PointerEvent) {
		wipeDragging = true;
		(e.target as HTMLElement).setPointerCapture(e.pointerId);
		updateWipePos(e);
	}

	function onWipePointerMove(e: PointerEvent) {
		if (!wipeDragging || !wipeViewport) return;
		updateWipePos(e);
	}

	function onWipePointerUp() {
		wipeDragging = false;
	}

	function updateWipePos(e: PointerEvent) {
		if (!wipeViewport) return;
		const rect = wipeViewport.getBoundingClientRect();
		const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
		wipePos = (x / rect.width) * 100;
	}

	let compareUrl = $derived(
		frameCount > 0 ? api.preview.url(clipName, comparePass, currentFrame) : null
	);

	let imgUrl = $derived(
		frameCount > 0 ? api.preview.url(clipName, selectedPass, currentFrame) : null
	);

	let videoUrl = $derived(
		frameCount > 0 ? `/api/preview/${encodeURIComponent(clipName)}/${selectedPass}/video?fps=${playbackFps}` : null
	);

	let downloadUrl = $derived(
		frameCount > 0 ? `/api/preview/${encodeURIComponent(clipName)}/${selectedPass}/download` : null
	);

	const passLabels: Record<string, string> = {
		input: 'Input',
		alpha: 'Alpha Hint',
		fg: 'FG',
		matte: 'Matte',
		comp: 'Comp',
		processed: 'Processed',
	};

	function onImgLoad() { loading = false; error = false; }
	function onImgError() { loading = false; error = true; }

	function onFrameChange(e: Event) {
		const target = e.target as HTMLInputElement;
		currentFrame = parseInt(target.value, 10);
		loading = true;
		error = false;
	}

	function switchPass(pass: string) {
		selectedPass = pass;
		if (mode === 'frame') {
			loading = true;
			error = false;
		}
	}

	// Zoom and pan state
	let zoom = $state(1);
	let panX = $state(0);
	let panY = $state(0);
	let isPanning = $state(false);
	let panStartX = 0;
	let panStartY = 0;
	let panOriginX = 0;
	let panOriginY = 0;
	let isZoomed = $derived(zoom > 1.05);

	let viewportTransform = $derived(
		`scale(${zoom}) translate(${panX / zoom}px, ${panY / zoom}px)`
	);

	function onWheel(e: WheelEvent) {
		if (mode === 'video') return;
		e.preventDefault();

		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		const mouseX = e.clientX - rect.left;
		const mouseY = e.clientY - rect.top;

		const prevZoom = zoom;
		const delta = e.deltaY > 0 ? 0.9 : 1.1;
		zoom = Math.max(1, Math.min(10, zoom * delta));

		// Zoom toward cursor
		if (zoom > 1) {
			const scale = zoom / prevZoom;
			panX = mouseX - scale * (mouseX - panX);
			panY = mouseY - scale * (mouseY - panY);
		} else {
			panX = 0;
			panY = 0;
		}
	}

	function onPanStart(e: PointerEvent) {
		if (mode === 'compare' && compareMode === 'wipe') return; // wipe uses its own drag
		if (!isZoomed) return;
		isPanning = true;
		panStartX = e.clientX;
		panStartY = e.clientY;
		panOriginX = panX;
		panOriginY = panY;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		e.preventDefault();
	}

	function onPanMove(e: PointerEvent) {
		if (!isPanning) return;
		panX = panOriginX + (e.clientX - panStartX);
		panY = panOriginY + (e.clientY - panStartY);
	}

	function onPanEnd() {
		isPanning = false;
	}

	function resetZoom() {
		zoom = 1;
		panX = 0;
		panY = 0;
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
		if (mode === 'video') return;
		switch (e.key) {
			case 'ArrowLeft': e.preventDefault(); currentFrame = Math.max(0, currentFrame - 1); break;
			case 'ArrowRight': e.preventDefault(); currentFrame = Math.min(frameCount - 1, currentFrame + 1); break;
			case 'Home': e.preventDefault(); currentFrame = 0; break;
			case 'End': e.preventDefault(); currentFrame = frameCount - 1; break;
			case '+': case '=': e.preventDefault(); zoom = Math.min(10, zoom * 1.2); break;
			case '-': e.preventDefault(); zoom = Math.max(1, zoom * 0.8); if (zoom <= 1.05) { panX = 0; panY = 0; } break;
			case '0': e.preventDefault(); resetZoom(); break;
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

<div class="viewer">
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="viewer-viewport"
		class:split={mode === 'compare' && compareMode === 'split'}
		class:wipe={mode === 'compare' && compareMode === 'wipe'}
		class:zoomed={isZoomed}
		bind:this={wipeViewport}
		onwheel={onWheel}
		onpointerdown={onPanStart}
		onpointermove={onPanMove}
		onpointerup={onPanEnd}
		ondblclick={resetZoom}
		style="--zoom-transform: {viewportTransform}"
	>
		{#if mode === 'compare' && compareMode === 'split'}
			<div class="compare-side">
				{#if compareNotReady}
					<div class="not-ready-overlay"><span class="mono">Not yet rendered</span></div>
				{:else if compareUrl}
					<img src={compareUrl} alt="Compare — {comparePass}" />
				{/if}
				<span class="compare-label mono">{passLabels[comparePass] ?? comparePass}</span>
			</div>
			<div class="compare-divider"></div>
			<div class="compare-side">
				{#if frameNotReady}
					<div class="not-ready-overlay"><span class="mono">Not yet rendered</span></div>
				{:else if imgUrl}
					<img src={imgUrl} alt="Frame {currentFrame} — {selectedPass}" />
				{/if}
				<span class="compare-label mono">{passLabels[selectedPass] ?? selectedPass}</span>
			</div>
		{:else if mode === 'compare' && compareMode === 'wipe'}
			<!-- Wipe: B layer (full) -->
			<div class="wipe-layer wipe-b">
				{#if frameNotReady}
					<div class="not-ready-overlay"><span class="mono">Not yet rendered</span></div>
				{:else if imgUrl}
					<img src={imgUrl} alt="Frame {currentFrame} — {selectedPass}" />
				{/if}
			</div>
			<!-- Wipe: A layer (clipped) -->
			<div class="wipe-layer wipe-a" style="clip-path: inset(0 {100 - wipePos}% 0 0)">
				{#if compareNotReady}
					<div class="not-ready-overlay"><span class="mono">Not yet rendered</span></div>
				{:else if compareUrl}
					<img src={compareUrl} alt="Compare — {comparePass}" />
				{/if}
			</div>
			<!-- Wipe divider -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="wipe-divider"
				style="left: {wipePos}%"
				onpointerdown={onWipePointerDown}
				onpointermove={onWipePointerMove}
				onpointerup={onWipePointerUp}
			>
				<div class="wipe-handle">
					<svg width="8" height="16" viewBox="0 0 8 16" fill="none"><path d="M2 4v8M6 4v8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
				</div>
			</div>
			<span class="compare-label mono wipe-label-a" style="left: 8px">{passLabels[comparePass] ?? comparePass}</span>
			<span class="compare-label mono wipe-label-b" style="right: 8px">{passLabels[selectedPass] ?? selectedPass}</span>
		{:else if mode === 'video' && (encodeStatus === 'encoding' || encodeStatus === 'stitching')}
			<div class="encode-progress">
				<div class="encode-label mono">
					{#if encodeStatus === 'stitching'}
						Stitching video...
					{:else}
						Converting frames... {encodeCurrent} / {encodeTotal}
					{/if}
				</div>
				<div class="encode-bar">
					<div
						class="encode-fill"
						style="width: {encodeTotal > 0 ? (encodeCurrent / encodeTotal) * 100 : 0}%"
					></div>
				</div>
				{#if encodeTotal > 0}
					<span class="encode-pct mono">{Math.round((encodeCurrent / encodeTotal) * 100)}%</span>
				{/if}
			</div>
		{:else if mode === 'video' && videoUrl}
			<!-- svelte-ignore a11y_media_has_caption -->
			<video
				src={videoUrl}
				controls
				loop
				autoplay
				class="video-player"
			></video>
		{:else if frameNotReady}
			<div class="not-ready-overlay">
				<span class="mono">Not yet rendered</span>
				<span class="not-ready-hint mono">Frame {currentFrame + 1} — processing up to {completedFrames}</span>
			</div>
		{:else if imgUrl && !error}
			<img
				src={imgUrl}
				alt="Frame {currentFrame} — {selectedPass}"
				class:loading
				onload={onImgLoad}
				onerror={onImgError}
			/>
		{/if}
		{#if loading && mode === 'frame'}
			<div class="overlay"><div class="spinner"></div></div>
		{/if}
		{#if error && mode === 'frame'}
			<div class="overlay"><span class="mono">Frame unavailable</span></div>
		{/if}
		{#if !imgUrl && !videoUrl}
			<div class="overlay"><span class="mono">No frames</span></div>
		{/if}
		{#if mode !== 'video' && frameCount > 0}
			<div class="frame-counter mono">{currentFrame + 1} / {frameCount}</div>
		{/if}
		{#if isZoomed}
			<button class="zoom-badge mono" onclick={resetZoom} title="Reset zoom (0)">
				{Math.round(zoom * 100)}%
			</button>
		{/if}
	</div>

	<div class="viewer-controls">
		<div class="controls-row">
			<div class="pass-tabs">
				{#each availablePasses as pass}
					<button
						class="pass-tab mono"
						class:active={selectedPass === pass}
						onclick={() => switchPass(pass)}
					>
						{passLabels[pass] ?? pass}
					</button>
				{/each}
			</div>

			<div class="mode-actions">
				{#if frameCount > 1}
					<div class="mode-toggle">
						<button class="mode-btn mono" class:active={mode === 'frame'} onclick={() => { mode = 'frame'; }}>
							Frames
						</button>
						<button class="mode-btn mono" class:active={mode === 'video'} onclick={() => { mode = 'video'; }}>
							Play
						</button>
						<button class="mode-btn mono" class:active={mode === 'compare'} onclick={() => { mode = 'compare'; }}>
							A/B
						</button>
					</div>
					{#if mode === 'compare'}
						<div class="mode-toggle">
							<button class="mode-btn mono" class:active={compareMode === 'split'} onclick={() => { compareMode = 'split'; }} title="Side by side">
								<svg width="14" height="10" viewBox="0 0 14 10" fill="none"><rect x="0.5" y="0.5" width="5.5" height="9" rx="0.5" stroke="currentColor" stroke-width="1"/><rect x="8" y="0.5" width="5.5" height="9" rx="0.5" stroke="currentColor" stroke-width="1"/></svg>
							</button>
							<button class="mode-btn mono" class:active={compareMode === 'wipe'} onclick={() => { compareMode = 'wipe'; }} title="Wipe">
								<svg width="14" height="10" viewBox="0 0 14 10" fill="none"><rect x="0.5" y="0.5" width="13" height="9" rx="0.5" stroke="currentColor" stroke-width="1"/><line x1="7" y1="0" x2="7" y2="10" stroke="currentColor" stroke-width="1.5"/></svg>
							</button>
						</div>
					{/if}
				{/if}
				{#if downloadUrl}
					<a href={downloadUrl} class="dl-btn mono" title="Download {passLabels[selectedPass] ?? selectedPass} as ZIP">
						<svg width="12" height="12" viewBox="0 0 14 14" fill="none">
							<path d="M7 2v7M3 9l4 3 4-3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
							<path d="M2 12h10" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
						</svg>
						{passLabels[selectedPass] ?? selectedPass}
					</a>
				{/if}
			</div>
		</div>

		{#if (mode === 'frame' || mode === 'compare') && frameCount > 1}
			<div class="scrub-row">
				<button class="tbtn" onclick={() => { currentFrame = Math.max(0, currentFrame - 1); }} title="Previous frame">
					<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M4 6l4-3v6z" fill="currentColor"/></svg>
				</button>
				<div class="scrub-wrap">
					{#if progressPct >= 0}
						<div class="scrub-progress" style="width: {progressPct}%"></div>
					{/if}
					<input
						type="range"
						min="0"
						max={frameCount - 1}
						value={currentFrame}
						oninput={onFrameChange}
						class="scrub-slider"
					/>
				</div>
				<button class="tbtn" onclick={() => { currentFrame = Math.min(frameCount - 1, currentFrame + 1); }} title="Next frame">
					<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M8 6l-4-3v6z" fill="currentColor"/></svg>
				</button>
			</div>
		{/if}

		{#if mode === 'compare'}
			<div class="compare-controls">
				<span class="compare-ctrl-label mono">COMPARE LEFT</span>
				<div class="pass-tabs">
					{#each availablePasses as pass}
						<button
							class="pass-tab mono"
							class:active={comparePass === pass}
							onclick={() => { comparePass = pass; }}
						>
							{passLabels[pass] ?? pass}
						</button>
					{/each}
				</div>
			</div>
		{/if}

		{#if mode === 'video' && frameCount > 1}
			<div class="fps-row">
				<span class="fps-label mono">FPS</span>
				<select bind:value={playbackFps} class="fps-select mono">
					<option value={8}>8</option>
					<option value={12}>12</option>
					<option value={24}>24</option>
					<option value={30}>30</option>
				</select>
				<span class="fps-hint mono">Change FPS to re-encode preview</span>
			</div>
		{/if}
	</div>
</div>

<style>
	.viewer {
		display: flex;
		flex-direction: column;
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		overflow: hidden;
		background: var(--surface-1);
		box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
	}

	.viewer-viewport {
		position: relative;
		aspect-ratio: 16 / 9;
		background:
			repeating-conic-gradient(var(--surface-3) 0% 25%, var(--surface-2) 0% 50%) 0 0 / 16px 16px;
		overflow: hidden;
	}

	.viewer-viewport img {
		width: 100%;
		height: 100%;
		object-fit: contain;
		transition: opacity 0.1s;
		transform: var(--zoom-transform, none);
		transform-origin: 0 0;
	}

	.viewer-viewport.zoomed {
		cursor: grab;
	}

	.viewer-viewport.zoomed:active {
		cursor: grabbing;
	}

	.viewer-viewport img.loading {
		opacity: 0.4;
	}

	.video-player {
		width: 100%;
		height: 100%;
		object-fit: contain;
		background: #000;
	}

	.viewer-viewport.split {
		display: flex;
	}

	.compare-side {
		flex: 1;
		position: relative;
		overflow: hidden;
	}

	.compare-side img {
		width: 100%;
		height: 100%;
		object-fit: contain;
	}

	.compare-label {
		position: absolute;
		top: 8px;
		left: 8px;
		padding: 2px 8px;
		font-size: 10px;
		font-weight: 600;
		color: var(--text-primary);
		background: rgba(0, 0, 0, 0.75);
		border-radius: var(--radius-sm);
		letter-spacing: 0.04em;
	}

	.compare-divider {
		width: 2px;
		background: var(--accent);
		flex-shrink: 0;
		box-shadow: 0 0 8px rgba(255, 242, 3, 0.3);
	}

	/* Wipe mode */
	.viewer-viewport.wipe {
		position: relative;
		cursor: ew-resize;
	}

	.wipe-layer {
		position: absolute;
		inset: 0;
	}

	.wipe-layer img {
		width: 100%;
		height: 100%;
		object-fit: contain;
	}

	.wipe-a {
		z-index: 2;
	}

	.wipe-b {
		z-index: 1;
	}

	.wipe-divider {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 20px;
		transform: translateX(-50%);
		z-index: 3;
		cursor: ew-resize;
		display: flex;
		align-items: center;
		justify-content: center;
		touch-action: none;
	}

	.wipe-divider::before {
		content: '';
		position: absolute;
		top: 0;
		bottom: 0;
		left: 50%;
		width: 2px;
		transform: translateX(-50%);
		background: var(--accent);
		box-shadow: 0 0 8px rgba(255, 242, 3, 0.4);
	}

	.wipe-handle {
		width: 16px;
		height: 28px;
		background: var(--accent);
		border-radius: 4px;
		display: flex;
		align-items: center;
		justify-content: center;
		color: #000;
		z-index: 1;
		box-shadow: 0 0 10px rgba(255, 242, 3, 0.3);
	}

	.wipe-label-a, .wipe-label-b {
		position: absolute;
		top: 8px;
		z-index: 4;
		padding: 2px 8px;
		font-size: 10px;
		font-weight: 600;
		color: var(--text-primary);
		background: rgba(0, 0, 0, 0.75);
		border-radius: var(--radius-sm);
		letter-spacing: 0.04em;
	}

	.compare-controls {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
	}

	.compare-ctrl-label {
		font-size: 9px;
		color: var(--text-tertiary);
		letter-spacing: 0.08em;
		flex-shrink: 0;
	}

	.overlay {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--text-tertiary);
		font-size: 13px;
	}

	.spinner {
		width: 20px;
		height: 20px;
		border: 2px solid var(--surface-4);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: spin 0.6s linear infinite;
	}

	.zoom-badge {
		position: absolute;
		top: var(--sp-2);
		right: var(--sp-2);
		padding: 3px 8px;
		font-size: 10px;
		font-weight: 600;
		color: var(--accent);
		background: rgba(0, 0, 0, 0.75);
		border: 1px solid var(--accent-muted);
		border-radius: var(--radius-sm);
		cursor: pointer;
		z-index: 5;
		transition: all 0.15s;
	}

	.zoom-badge:hover {
		background: var(--accent);
		color: #000;
	}

	.frame-counter {
		position: absolute;
		bottom: var(--sp-2);
		right: var(--sp-2);
		padding: 2px 6px;
		font-size: 10px;
		color: var(--text-primary);
		background: rgba(0, 0, 0, 0.7);
		border-radius: var(--radius-sm);
		pointer-events: none;
	}

	@keyframes spin { to { transform: rotate(360deg); } }

	.viewer-controls {
		padding: var(--sp-3);
		display: flex;
		flex-direction: column;
		gap: var(--sp-3);
		border-top: 1px solid var(--border);
		background: var(--surface-2);
	}

	.controls-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--sp-3);
	}

	.pass-tabs {
		display: flex;
		gap: 4px;
		flex-wrap: wrap;
	}

	.pass-tab {
		padding: 6px 10px;
		min-height: 28px;
		font-size: 10px;
		font-weight: 500;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--surface-3);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.1s;
	}

	.pass-tab:hover { color: var(--text-primary); border-color: var(--text-tertiary); }
	.pass-tab.active { color: var(--accent); border-color: var(--accent); background: var(--accent-muted); }

	.mode-actions {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		flex-shrink: 0;
	}

	.mode-toggle {
		display: flex;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		overflow: hidden;
	}

	.mode-btn {
		padding: 6px 10px;
		min-height: 28px;
		font-size: 10px;
		font-weight: 500;
		border: none;
		background: var(--surface-3);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.1s;
	}

	.mode-btn:first-child { border-right: 1px solid var(--border); }
	.mode-btn:hover { color: var(--text-primary); }
	.mode-btn.active { color: var(--accent); background: var(--accent-muted); }

	.dl-btn {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 3px 8px;
		height: 24px;
		font-size: 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--text-secondary);
		transition: all 0.1s;
	}

	.dl-btn:hover { color: var(--accent); border-color: var(--accent); }

	.encode-progress {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--sp-2);
		background: rgba(0, 0, 0, 0.6);
	}

	.encode-label {
		font-size: 12px;
		color: var(--text-secondary);
	}

	.encode-bar {
		width: 200px;
		height: 4px;
		background: var(--surface-4);
		border-radius: 2px;
		overflow: hidden;
	}

	.encode-fill {
		height: 100%;
		background: var(--accent);
		border-radius: 2px;
		transition: width 0.3s ease-out;
	}

	.encode-pct {
		font-size: 11px;
		color: var(--accent);
		font-weight: 600;
	}

	.not-ready-overlay {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 4px;
		color: var(--text-tertiary);
		font-size: 13px;
		background: rgba(0, 0, 0, 0.5);
	}

	.not-ready-hint {
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.scrub-row {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.scrub-wrap {
		flex: 1;
		position: relative;
		height: 16px;
		display: flex;
		align-items: center;
	}

	.scrub-progress {
		position: absolute;
		left: 0;
		top: 50%;
		transform: translateY(-50%);
		height: 4px;
		background: var(--accent-dim);
		border-radius: 2px;
		opacity: 0.4;
		pointer-events: none;
		transition: width 0.3s ease-out;
	}

	.tbtn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 22px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--surface-3);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.1s;
		flex-shrink: 0;
	}

	.tbtn:hover { color: var(--text-primary); background: var(--surface-4); }

	.scrub-slider {
		flex: 1;
		-webkit-appearance: none;
		appearance: none;
		height: 4px;
		background: var(--surface-4);
		border-radius: 2px;
		outline: none;
		cursor: pointer;
	}

	.scrub-slider::-webkit-slider-thumb {
		-webkit-appearance: none;
		width: 12px;
		height: 12px;
		border-radius: 50%;
		background: var(--accent);
		cursor: pointer;
		border: 2px solid var(--surface-2);
	}

	.scrub-slider::-moz-range-thumb {
		width: 12px;
		height: 12px;
		border-radius: 50%;
		background: var(--accent);
		cursor: pointer;
		border: 2px solid var(--surface-2);
	}

	.fps-row {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
	}

	.fps-label {
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.fps-select {
		padding: 2px 6px;
		font-size: 10px;
		background: var(--surface-3);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--text-secondary);
		cursor: pointer;
	}

	.fps-hint {
		font-size: 9px;
		color: var(--text-tertiary);
		margin-left: auto;
	}
</style>
