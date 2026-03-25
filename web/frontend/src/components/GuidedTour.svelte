<script lang="ts">
	import { page } from '$app/state';
	import { onMount } from 'svelte';

	const STORAGE_KEY = 'ck:tour';

	interface TourStep {
		id: string;
		selector: string; // CSS selector for the target element
		title: string;
		text: string;
		position: 'bottom' | 'right' | 'left' | 'top';
		page?: string; // only show on this page (path prefix)
	}

	// All possible tour steps — shown contextually based on page
	const ALL_STEPS: TourStep[] = [
		// Sidebar tour (shown on first login, any page)
		{ id: 'nav-clips', selector: 'a[href="/clips"]', title: 'CLIPS', text: 'Your footage lives here. Upload videos or images to start keying.', position: 'right' },
		{ id: 'nav-jobs', selector: 'a[href="/jobs"]', title: 'JOBS', text: 'Track processing progress. See running, queued, and completed jobs.', position: 'right' },
		{ id: 'nav-nodes', selector: 'a[href="/nodes"]', title: 'NODES', text: 'Connect GPU nodes to the render farm. More GPUs = faster processing.', position: 'right' },
		{ id: 'nav-settings', selector: 'a[href="/settings"]', title: 'SETTINGS', text: 'Configure inference parameters and output format defaults.', position: 'right' },

		// Clips page
		{ id: 'clips-upload', selector: '.upload-label, .btn-upload', title: 'UPLOAD', text: 'Drag & drop green screen footage here, or click to browse. Supports video (MP4, MOV), images (PNG, EXR, JPG), and ZIP archives.', position: 'bottom', page: '/clips' },

		// Clip detail page
		{ id: 'clip-pipeline', selector: '.btn-hero, .btn-muted', title: 'RUN PIPELINE', text: 'Click "Run Full Pipeline" to automatically generate alpha hints and key your footage. It distributes work across all available GPUs.', position: 'left', page: '/clips/' },
		{ id: 'clip-passes', selector: '.pass-selector, .download-passes', title: 'OUTPUT PASSES', text: 'Processed = comp-ready RGBA EXR. Comp = PNG preview. Enable FG/Matte in settings for separate foreground and alpha passes.', position: 'left', page: '/clips/' },

		// Jobs page
		{ id: 'jobs-running', selector: '.section-title, .job-list', title: 'JOB QUEUE', text: 'Running jobs show real-time progress. Queued jobs show position and estimated wait time. Click any job to see its log.', position: 'bottom', page: '/jobs' },

		// Nodes page
		{ id: 'nodes-setup', selector: '.setup-step, .token-generate', title: 'ADD A NODE', text: 'Generate a token and follow the Docker setup guide to connect a GPU. Your node earns credits for every job it processes.', position: 'bottom', page: '/nodes' },

		// Settings page
		{ id: 'settings-outputs', selector: '.output-toggles, .inference-form', title: 'OUTPUT CONFIG', text: 'Choose which passes to generate. Processed + Comp is the default. Enable FG/Matte for advanced compositing workflows.', position: 'bottom', page: '/settings' },
	];

	let currentStep = $state<TourStep | null>(null);
	let tooltipStyle = $state('');
	let dismissed = $state<Set<string>>(new Set());

	function loadDismissed(): Set<string> {
		try {
			const raw = localStorage.getItem(STORAGE_KEY);
			return raw ? new Set(JSON.parse(raw)) : new Set();
		} catch {
			return new Set();
		}
	}

	function saveDismissed() {
		localStorage.setItem(STORAGE_KEY, JSON.stringify([...dismissed]));
	}

	function dismissStep() {
		if (currentStep) {
			dismissed.add(currentStep.id);
			saveDismissed();
		}
		// Find next undismissed step for current page
		showNextStep();
	}

	function dismissAll() {
		for (const step of ALL_STEPS) dismissed.add(step.id);
		saveDismissed();
		currentStep = null;
	}

	function showNextStep() {
		const currentPath = page.url.pathname;

		for (const step of ALL_STEPS) {
			if (dismissed.has(step.id)) continue;
			if (step.page && !currentPath.startsWith(step.page)) continue;

			const el = document.querySelector(step.selector);
			if (!el) continue;

			currentStep = step;
			positionTooltip(el as HTMLElement, step.position);
			return;
		}
		currentStep = null;
	}

	function positionTooltip(target: HTMLElement, position: string) {
		const rect = target.getBoundingClientRect();
		const gap = 12;

		let top = 0;
		let left = 0;

		if (position === 'right') {
			top = rect.top + rect.height / 2;
			left = rect.right + gap;
		} else if (position === 'bottom') {
			top = rect.bottom + gap;
			left = rect.left + rect.width / 2;
		} else if (position === 'left') {
			top = rect.top + rect.height / 2;
			left = rect.left - gap;
		} else if (position === 'top') {
			top = rect.top - gap;
			left = rect.left + rect.width / 2;
		}

		tooltipStyle = `top: ${top}px; left: ${left}px;`;
	}

	onMount(() => {
		dismissed = loadDismissed();
		// Small delay to ensure DOM is rendered
		setTimeout(showNextStep, 500);
	});

	// Re-check when page changes — delay to let DOM render
	let _lastPath = '';
	$effect(() => {
		const path = page.url.pathname;
		if (path !== _lastPath) {
			_lastPath = path;
			// Wait for page DOM to render before looking for elements
			setTimeout(showNextStep, 600);
		}
	});

	export function reset() {
		dismissed = new Set();
		localStorage.removeItem(STORAGE_KEY);
		setTimeout(showNextStep, 100);
	}
</script>

{#if currentStep}
<div class="tour-overlay" onclick={dismissAll}>
	<div
		class="tour-tooltip {currentStep.position}"
		style={tooltipStyle}
		onclick={(e) => e.stopPropagation()}
	>
		<div class="tour-header">
			<span class="tour-title mono">{currentStep.title}</span>
			<button class="tour-skip mono" onclick={dismissAll}>SKIP TOUR</button>
		</div>
		<p class="tour-text">{currentStep.text}</p>
		<button class="tour-next mono" onclick={dismissStep}>
			GOT IT
			<svg width="10" height="10" viewBox="0 0 14 14" fill="none"><path d="M3 7h8M8 4l3 3-3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
		</button>
	</div>
</div>
{/if}

<style>
	.tour-overlay {
		position: fixed;
		inset: 0;
		z-index: 8500;
		background: rgba(0, 0, 0, 0.5);
		animation: tour-fade 0.2s ease;
	}

	@keyframes tour-fade {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	.tour-tooltip {
		position: fixed;
		z-index: 8501;
		background: var(--surface-2);
		border: 1px solid var(--border-active);
		border-radius: var(--radius-md);
		padding: var(--sp-4);
		max-width: 280px;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5), 0 0 0 1px var(--accent-muted);
		animation: tour-pop 0.2s ease;
	}

	@keyframes tour-pop {
		from { opacity: 0; transform: scale(0.95); }
		to { opacity: 1; transform: scale(1); }
	}

	.tour-tooltip.right { transform: translateY(-50%); }
	.tour-tooltip.left { transform: translate(-100%, -50%); }
	.tour-tooltip.bottom { transform: translateX(-50%); }
	.tour-tooltip.top { transform: translate(-50%, -100%); }

	/* Arrow */
	.tour-tooltip::before {
		content: '';
		position: absolute;
		width: 8px;
		height: 8px;
		background: var(--surface-2);
		border: 1px solid var(--border-active);
		transform: rotate(45deg);
	}
	.tour-tooltip.right::before { left: -5px; top: 50%; margin-top: -4px; border-right: none; border-top: none; }
	.tour-tooltip.left::before { right: -5px; top: 50%; margin-top: -4px; border-left: none; border-bottom: none; }
	.tour-tooltip.bottom::before { top: -5px; left: 50%; margin-left: -4px; border-bottom: none; border-right: none; }
	.tour-tooltip.top::before { bottom: -5px; left: 50%; margin-left: -4px; border-top: none; border-left: none; }

	.tour-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: var(--sp-2);
	}

	.tour-title {
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.12em;
		color: var(--accent);
	}

	.tour-skip {
		font-size: 9px;
		letter-spacing: 0.06em;
		color: var(--text-tertiary);
		background: none;
		border: none;
		cursor: pointer;
		padding: 2px 4px;
	}
	.tour-skip:hover { color: var(--text-secondary); }

	.tour-text {
		font-size: 13px;
		line-height: 1.5;
		color: var(--text-secondary);
		margin-bottom: var(--sp-3);
	}

	.tour-next {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.08em;
		padding: 6px 14px;
		background: var(--accent);
		color: #000;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition: background 0.15s;
	}
	.tour-next:hover { background: #fff; }
</style>
