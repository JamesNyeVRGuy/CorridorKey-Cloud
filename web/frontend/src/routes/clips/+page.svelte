<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { clips, refreshClips } from '$lib/stores/clips';
	import { activeOrgId } from '$lib/stores/orgs';
	import { refreshJobs } from '$lib/stores/jobs';
	import { autoExtractFrames, autoShard } from '$lib/stores/settings';
	import { api } from '$lib/api';
	import type { Project, Clip } from '$lib/api';
	import type { InferenceParams, OutputConfig } from '$lib/api';
	import { defaultParams, defaultOutputConfig } from '$lib/stores/settings';
	import ClipCard from '../../components/ClipCard.svelte';
	import ContextMenu from '../../components/ContextMenu.svelte';
	import type { MenuItem } from '../../components/ContextMenu.svelte';
	import { toast } from '$lib/stores/toasts';

	let projects = $state<Project[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let uploading = $state(false);
	let uploadProgress = $state(0); // 0-100
	let uploadError = $state<string | null>(null);
	let dragOver = $state(false);
	let creatingProject = $state(false);
	let newProjectName = $state('');
	let showCreateForm = $state(false);

	// Drag state for visual drop zones
	let draggingClip = $state(false);
	let dropTargetProject = $state<string | null>(null);

	// Upload modal state
	let showUploadModal = $state(false);
	let pendingFiles = $state<File[]>([]);
	let uploadProjectName = $state('');
	let uploadProjectMode = $state<'new' | 'existing'>('new');
	let uploadSelectedProject = $state('');
	let uploadFolderMode = $state<'none' | 'existing' | 'new'>('none');
	let uploadSelectedFolder = $state('');
	let uploadNewFolderName = $state('');
	let uploadStatus = $state<'choose' | 'uploading' | 'extracting' | 'done'>('choose');
	let uploadFileProgress = $state<{ name: string; progress: number; done: boolean }[]>([]);

	// Multi-select
	let selectedClips = $state<Set<string>>(new Set());

	function toggleSelect(clipName: string, e?: MouseEvent) {
		const next = new Set(selectedClips);
		if (next.has(clipName)) next.delete(clipName);
		else next.add(clipName);
		selectedClips = next;
		e?.stopPropagation();
	}

	function clearSelection() {
		selectedClips = new Set();
	}

	let hasSelection = $derived(selectedClips.size > 0);

	function showSelectionContext(e: MouseEvent) {
		e.preventDefault();
		const names = [...selectedClips];
		ctxX = e.clientX;
		ctxY = e.clientY;
		ctxItems = [
			{
				label: `Run Pipeline (${names.length} clips)`,
				action: async () => {
					try {
						await api.jobs.submitPipeline(names);
						toast.success(`Pipeline started for ${names.length} clips`);
						await refreshJobs();
						clearSelection();
					} catch (err) {
						toast.error(err instanceof Error ? err.message : String(err));
					}
				},
			},
			{
				label: `Run Inference (${names.length} clips)`,
				action: async () => {
					try {
						if ($autoShard) {
							await api.jobs.submitShardedInference(names, $defaultParams, $defaultOutputConfig);
						} else {
							await api.jobs.submitInference(names, $defaultParams, $defaultOutputConfig);
						}
						toast.success(`Inference started for ${names.length} clips`);
						await refreshJobs();
						clearSelection();
					} catch (err) {
						toast.error(err instanceof Error ? err.message : String(err));
					}
				},
			},
			{
				label: `Run GVM Alpha (${names.length} clips)`,
				action: async () => {
					try {
						await api.jobs.submitGVM(names);
						toast.success(`GVM started for ${names.length} clips`);
						await refreshJobs();
						clearSelection();
					} catch (err) {
						toast.error(err instanceof Error ? err.message : String(err));
					}
				},
			},
			{
				label: `Run VideoMaMa (${names.length} clips)`,
				action: async () => {
					try {
						await api.jobs.submitVideoMaMa(names);
						toast.success(`VideoMaMa started for ${names.length} clips`);
						await refreshJobs();
						clearSelection();
					} catch (err) {
						toast.error(err instanceof Error ? err.message : String(err));
					}
				},
			},
			{ label: '---', action: () => {} },
			{
				label: `Delete Selected (${names.length})`,
				danger: true,
				action: async () => {
					if (!confirm(`Delete ${names.length} selected clips? This cannot be undone.`)) return;
					for (const n of names) {
						try { await api.clips.delete(n); } catch { /* continue */ }
					}
					clearSelection();
					await loadProjects();
				},
			},
			{ label: '---', action: () => {} },
			{
				label: 'Clear Selection',
				action: clearSelection,
			},
		];
		ctxVisible = true;
	}

	// Context menu state
	let ctxVisible = $state(false);
	let ctxX = $state(0);
	let ctxY = $state(0);
	let ctxItems = $state<MenuItem[]>([]);

	function showProjectContext(e: MouseEvent, project: Project) {
		e.preventDefault();
		ctxX = e.clientX;
		ctxY = e.clientY;
		const clipNames = project.clips.map(c => c.name);
		ctxItems = [
			{
				label: `Process All (${project.clip_count} clips)`,
				disabled: project.clip_count === 0,
				action: async () => {
					try {
						await api.jobs.submitPipeline(clipNames);
						toast.success(`Pipeline started for ${clipNames.length} clips`);
						await refreshJobs();
					} catch (e) {
						toast.error(e instanceof Error ? e.message : String(e));
					}
				},
			},
			{ label: '---', action: () => {} },
			{
				label: 'Rename',
				action: async () => {
					const name = prompt('New project name:', project.display_name);
					if (name && name.trim()) {
						await api.projects.rename(project.name, name.trim());
						await loadProjects();
					}
				},
			},
			{ label: '---', action: () => {} },
			{
				label: `Delete All Clips (${project.clip_count})`,
				danger: true,
				disabled: project.clip_count === 0,
				action: async () => {
					if (!confirm(`Delete all ${project.clip_count} clips in "${project.display_name}"? This cannot be undone.`)) return;
					for (const name of clipNames) {
						try { await api.clips.delete(name); } catch { /* continue */ }
					}
					await loadProjects();
				},
			},
			{
				label: 'Delete Project',
				danger: true,
				action: () => deleteProject(project.name, project.display_name),
			},
		];
		ctxVisible = true;
	}

	function showClipContext(e: MouseEvent, clip: Clip, project: Project) {
		e.preventDefault();
		const otherProjects = projects.filter(p => p.name !== project.name);

		// Move to other projects
		const moveProjectItems: MenuItem[] = otherProjects.map(p => ({
			label: `📁 ${p.display_name}`,
			action: async () => {
				await api.clips.move(clip.name, p.name);
				await Promise.all([loadProjects(), refreshClips()]);
			},
		}));

		// Move to folders within current project
		const currentFolders = project.folders || [];
		const moveFolderItems: MenuItem[] = [];

		// "Move to loose clips" option (if clip is in a folder)
		if (clip.folder_name) {
			moveFolderItems.push({
				label: '↑ Loose clips (no folder)',
				action: async () => {
					await api.clips.move(clip.name, project.name);
					await Promise.all([loadProjects(), refreshClips()]);
				},
			});
		}

		// Move to existing folders in this project
		for (const f of currentFolders) {
			if (f.name === clip.folder_name) continue; // skip current folder
			moveFolderItems.push({
				label: `📂 ${f.display_name}`,
				action: async () => {
					await api.clips.move(clip.name, project.name, f.name);
					await Promise.all([loadProjects(), refreshClips()]);
				},
			});
		}

		// Create new folder and move
		moveFolderItems.push({
			label: '+ New folder...',
			action: async () => {
				const name = prompt('Folder name:');
				if (!name?.trim()) return;
				try {
					const folder = await api.projects.createFolder(project.name, name.trim());
					await api.clips.move(clip.name, project.name, folder.name);
					await Promise.all([loadProjects(), refreshClips()]);
				} catch (err) {
					toast.error(err instanceof Error ? err.message : String(err));
				}
			},
		});

		const allMoveItems: MenuItem[] = [];
		if (moveFolderItems.length > 0) {
			allMoveItems.push({ label: 'Move to folder', disabled: true, action: () => {} });
			allMoveItems.push(...moveFolderItems);
		}
		if (moveProjectItems.length > 0) {
			allMoveItems.push({ label: 'Move to project', disabled: true, action: () => {} });
			allMoveItems.push(...moveProjectItems);
		}

		ctxItems = [
			{
				label: 'Open',
				action: () => goto(`/clips/${encodeURIComponent(clip.name)}`),
			},
			{
				label: 'Run Full Pipeline',
				disabled: clip.state === 'COMPLETE',
				action: async () => {
					await api.jobs.submitPipeline([clip.name]);
					await refreshJobs();
				},
			},
			{ label: '---', action: () => {} },
			...(allMoveItems.length > 0
				? [...allMoveItems, { label: '---', action: () => {} }]
				: []),
			{
				label: 'Delete Clip',
				danger: true,
				action: async () => {
					if (confirm(`Delete clip "${clip.name}"?`)) {
						await api.clips.delete(clip.name);
						await loadProjects();
					}
				},
			},
		];
		ctxX = e.clientX;
		ctxY = e.clientY;
		ctxVisible = true;
	}
	function _loadCollapsed(): Set<string> {
		try { const raw = localStorage.getItem('ck:collapsed_projects'); return raw ? new Set(JSON.parse(raw)) : new Set(); }
		catch { return new Set(); }
	}
	function _saveCollapsed(s: Set<string>) { localStorage.setItem('ck:collapsed_projects', JSON.stringify([...s])); }
	let collapsedProjects = $state<Set<string>>(_loadCollapsed());

	// Search + state filter
	let searchQuery = $state('');
	let stateFilter = $state<string>('all');

	const STATES = ['RAW', 'READY', 'COMPLETE', 'ERROR', 'EXTRACTING', 'MASKED'];

	function _matchClip(c: Clip, q: string, projectName: string): boolean {
		if (stateFilter !== 'all' && c.state !== stateFilter) return false;
		if (q && !c.name.toLowerCase().includes(q) && !projectName.toLowerCase().includes(q)) return false;
		return true;
	}

	let filteredProjects = $derived.by(() => {
		if (!searchQuery && stateFilter === 'all') return projects;
		const q = searchQuery.toLowerCase();
		return projects.map(p => {
			const filteredClips = p.clips.filter(c => _matchClip(c, q, p.display_name));
			const filteredFolders = (p.folders || []).map(f => ({
				...f,
				clips: f.clips.filter(c => _matchClip(c, q, p.display_name)),
			})).filter(f => f.clips.length > 0);
			const totalClips = filteredClips.length + filteredFolders.reduce((s, f) => s + f.clips.length, 0);
			return { ...p, clips: filteredClips, folders: filteredFolders, clip_count: totalClips };
		}).filter(p => p.clip_count > 0 || (!q && stateFilter === 'all'));
	});

	const VIDEO_EXTS = ['.mp4', '.mov', '.avi', '.mkv', '.mxf', '.webm', '.m4v'];
	const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.exr', '.tif', '.tiff', '.bmp', '.dpx'];
	const isVideo = (name: string) => VIDEO_EXTS.some(ext => name.toLowerCase().endsWith(ext));
	const isImage = (name: string) => IMAGE_EXTS.some(ext => name.toLowerCase().endsWith(ext));
	const isZip = (name: string) => name.toLowerCase().endsWith('.zip');

	async function loadProjects() {
		loading = true;
		error = null;
		try {
			projects = await api.projects.list();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	function handleFiles(files: FileList | File[]) {
		const valid = Array.from(files).filter(f => isVideo(f.name) || isImage(f.name) || isZip(f.name));
		if (valid.length === 0) { uploadError = 'No supported files. Use videos, images, or zipped frames.'; return; }
		pendingFiles = valid;
		// Default project name from first file
		const firstName = valid[0].name.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' ');
		uploadProjectName = firstName;
		uploadStatus = 'choose';
		uploadProjectMode = projects.length > 0 ? 'existing' : 'new';
		uploadSelectedProject = projects.length > 0 ? projects[0].name : '';
		uploadFileProgress = valid.map(f => ({ name: f.name, progress: 0, done: false }));
		showUploadModal = true;
	}

	async function startUpload() {
		let targetProject: string | undefined;
		let targetFolder: string | undefined;
		const displayName = uploadProjectMode === 'existing'
			? (projects.find(p => p.name === uploadSelectedProject)?.display_name || '')
			: uploadProjectName.trim();

		if (uploadProjectMode === 'existing') {
			targetProject = uploadSelectedProject;
			if (uploadFolderMode === 'existing' && uploadSelectedFolder) {
				targetFolder = uploadSelectedFolder;
			} else if (uploadFolderMode === 'new' && uploadNewFolderName.trim()) {
				// Create folder first
				try {
					const f = await api.projects.createFolder(uploadSelectedProject, uploadNewFolderName.trim());
					targetFolder = f.name;
				} catch { /* will upload to loose clips */ }
			}
		}
		// For new projects: don't set targetProject — let the first upload create it
		// Subsequent files will use the created project name
		uploadStatus = 'uploading';
		uploading = true;
		uploadError = null;
		let lastUploadedClips: string[] = [];
		try {
			const imageFiles = pendingFiles.filter(f => isImage(f.name));
			const otherFiles = pendingFiles.filter(f => !isImage(f.name));

			if (imageFiles.length > 0) {
				const idx = pendingFiles.indexOf(imageFiles[0]);
				const result = await api.upload.images(imageFiles, targetProject || undefined, (loaded, total) => {
					if (idx >= 0) uploadFileProgress[idx] = { ...uploadFileProgress[idx], progress: Math.round((loaded / total) * 100) };
				}, targetFolder || undefined);
				if (result?.clips) for (const c of result.clips) { if (c.name) lastUploadedClips.push(c.name); }
				for (const f of imageFiles) {
					const i = pendingFiles.indexOf(f);
					if (i >= 0) uploadFileProgress[i] = { ...uploadFileProgress[i], done: true, progress: 100 };
				}
			}

			// Upload files — pass project/folder to API so clips go into the right place
			for (let fi = 0; fi < otherFiles.length; fi++) {
				const file = otherFiles[fi];
				const idx = pendingFiles.indexOf(file);
				let result: any;
				const onProgress = (loaded: number, total: number) => {
					if (idx >= 0) uploadFileProgress[idx] = { ...uploadFileProgress[idx], progress: Math.round((loaded / total) * 100) };
				};
				// First file may create the project; subsequent files use the created project
				const uploadName = !targetProject && fi === 0 ? (displayName || undefined) : undefined;
				if (isVideo(file.name)) {
					result = await api.upload.video(file, uploadName, $autoExtractFrames, onProgress, targetProject, targetFolder);
				} else if (isZip(file.name)) {
					result = await api.upload.frames(file, uploadName, onProgress);
				} else { continue; }
				if (result?.clips) {
					for (const c of result.clips) {
						if (c.name) lastUploadedClips.push(c.name);
						// After first upload in "new" mode, find the project it created
						// so subsequent files go into the same project
						if (fi === 0 && !targetProject) {
							const refreshed = await api.projects.list();
							const allClips = refreshed.flatMap(p => [...p.clips, ...p.folders.flatMap(f => f.clips)]);
							const match = refreshed.find(p =>
								p.clips.some(cl => cl.name === c.name) ||
								p.folders.some(f => f.clips.some(cl => cl.name === c.name))
							);
							if (match) targetProject = match.name;
						}
					}
				}
				if (idx >= 0) uploadFileProgress[idx] = { ...uploadFileProgress[idx], done: true, progress: 100 };
			}

			uploadStatus = $autoExtractFrames ? 'extracting' : 'done';
			await Promise.all([loadProjects(), refreshClips(), refreshJobs()]);

			// Auto-close after a moment if extraction is queued
			if (uploadStatus === 'extracting') {
				setTimeout(() => { uploadStatus = 'done'; }, 2000);
			}
			setTimeout(() => {
				showUploadModal = false;
				if (lastUploadedClips.length === 1) goto(`/clips/${encodeURIComponent(lastUploadedClips[0])}`);
			}, uploadStatus === 'done' ? 500 : 3000);
		} catch (e) {
			uploadError = e instanceof Error ? e.message : String(e);
			uploadStatus = 'done';
		} finally {
			uploading = false;
		}
	}

	async function createProject() {
		if (!newProjectName.trim()) return;
		creatingProject = true;
		try {
			await api.projects.create(newProjectName.trim());
			newProjectName = '';
			showCreateForm = false;
			await loadProjects();
		} catch (e) {
			uploadError = e instanceof Error ? e.message : String(e);
		} finally {
			creatingProject = false;
		}
	}

	async function deleteProject(name: string, displayName: string) {
		if (!confirm(`Delete project "${displayName}" and ALL clips inside it? This cannot be undone.`)) return;
		try {
			await api.projects.delete(name);
			await Promise.all([loadProjects(), refreshClips()]);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		}
	}

	function toggleProject(name: string) {
		const next = new Set(collapsedProjects);
		if (next.has(name)) next.delete(name);
		else next.add(name);
		collapsedProjects = next;
		_saveCollapsed(next);
	}

	function onDrop(e: DragEvent) { e.preventDefault(); dragOver = false; if (e.dataTransfer?.files.length) handleFiles(e.dataTransfer.files); }
	function onDragOver(e: DragEvent) { e.preventDefault(); dragOver = true; }
	function onDragLeave() { dragOver = false; }
	function onFileInput(e: Event) { const input = e.target as HTMLInputElement; if (input.files?.length) { handleFiles(input.files); input.value = ''; } }
	function onCreateKeydown(e: KeyboardEvent) { if (e.key === 'Enter') createProject(); if (e.key === 'Escape') showCreateForm = false; }

	let totalClips = $derived(projects.reduce((sum, p) => sum + p.clips.length, 0));

	onMount(loadProjects);

	// Reload when active org changes (org switcher)
	let _prevOrg = $activeOrgId;
	$effect(() => {
		if ($activeOrgId && $activeOrgId !== _prevOrg) {
			_prevOrg = $activeOrgId;
			loadProjects();
		}
	});

	// Reload projects when global clips store changes (WS clip:state_changed)
	let _prevClipCount = $clips.length;
	$effect(() => {
		const current = $clips;
		// Detect state changes by comparing serialized states
		const changed = current.length !== _prevClipCount ||
			current.some((c, i) => {
				const prev = projects.flatMap(p => p.clips);
				const match = prev.find(p => p.name === c.name);
				return match && match.state !== c.state;
			});
		if (changed && !loading) {
			_prevClipCount = current.length;
			loadProjects();
		}
	});
</script>

<svelte:head>
	<title>Clips — CorridorKey</title>
</svelte:head>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="page" class:drag-over={dragOver} ondrop={onDrop} ondragover={onDragOver} ondragleave={onDragLeave}>
	<div class="page-header">
		<div class="header-left">
			<h1 class="page-title">Projects</h1>
			{#if !loading}
				<span class="header-count mono">{projects.length} projects &middot; {totalClips} clips</span>
			{/if}
		</div>
		<div class="header-actions">
			<button class="btn-ghost" onclick={() => { showCreateForm = !showCreateForm; }} title="New project">
				<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2v10M2 7h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
				New Project
			</button>
			<label class="btn-accent" class:disabled={uploading}>
				<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2v8M3 6l4-4 4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 11h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
				{uploading ? `Uploading ${uploadProgress}%` : 'Upload'}
				<input type="file" accept=".mp4,.mov,.avi,.mkv,.mxf,.webm,.m4v,.zip,.png,.jpg,.jpeg,.exr,.tif,.tiff,.bmp,.dpx" multiple hidden oninput={onFileInput} disabled={uploading} />
			</label>
			<button class="btn-ghost" onclick={loadProjects} disabled={loading} aria-label="Refresh clips">
				<svg width="14" height="14" viewBox="0 0 14 14" fill="none" class:spinning={loading}><path d="M12 7a5 5 0 11-1.5-3.5M12 2v3h-3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
			</button>
		</div>
	</div>

	{#if showCreateForm}
		<div class="create-form">
			<input
				type="text"
				class="create-input"
				placeholder="Project name..."
				bind:value={newProjectName}
				onkeydown={onCreateKeydown}
				disabled={creatingProject}
			/>
			<button class="btn-sm" onclick={createProject} disabled={creatingProject || !newProjectName.trim()}>
				{creatingProject ? 'Creating...' : 'Create'}
			</button>
			<button class="btn-ghost-sm" onclick={() => { showCreateForm = false; }}>Cancel</button>
		</div>
	{/if}

	<!-- Search + state filter -->
	<div class="filter-bar">
		<input type="text" class="filter-search mono" placeholder="Search clips or projects..." bind:value={searchQuery} />
		<div class="state-toggles">
			<button class="state-btn mono" class:active={stateFilter === 'all'} onclick={() => stateFilter = 'all'}>All</button>
			{#each STATES as s}
				<button class="state-btn mono" class:active={stateFilter === s} data-state={s} onclick={() => stateFilter = stateFilter === s ? 'all' : s}>{s}</button>
			{/each}
		</div>
	</div>

	{#if hasSelection}
		<div class="selection-bar">
			<span class="selection-count mono">{selectedClips.size} clip{selectedClips.size !== 1 ? 's' : ''} selected</span>
			<button class="btn-ghost-sm" onclick={(e) => showSelectionContext(e)}>
				Actions
			</button>
			<button class="btn-ghost-sm" onclick={clearSelection}>
				Clear
			</button>
		</div>
	{/if}

	{#if error || uploadError}
		<div class="error-banner mono">
			{error || uploadError}
		</div>
	{/if}

	{#if uploading}
		<div class="upload-bar">
			<div class="upload-bar-fill" style="width: {uploadProgress}%"></div>
			<span class="upload-bar-text mono">Uploading {uploadProgress}%</span>
		</div>
	{/if}

	{#if dragOver}
		<div class="drop-overlay">
			<div class="drop-content">
				<svg width="36" height="36" viewBox="0 0 36 36" fill="none"><path d="M18 6v18M10 14l8-8 8 8" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M5 28h26" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round"/></svg>
				<span class="drop-text">Drop videos or zipped frames</span>
			</div>
		</div>
	{/if}

	{#if filteredProjects.length === 0 && !loading}
		<div class="empty-state">
			{#if searchQuery || stateFilter !== 'all'}
				<p class="empty-text">No clips match your filters</p>
				<button class="btn-ghost-sm mono" onclick={() => { searchQuery = ''; stateFilter = 'all'; }}>Clear filters</button>
			{:else}
				<svg width="48" height="48" viewBox="0 0 48 48" fill="none">
					<rect x="6" y="10" width="36" height="28" rx="4" stroke="var(--text-tertiary)" stroke-width="1.5"/>
					<path d="M14 10v28M34 10v28M6 19h8M34 19h8M6 29h8M34 29h8" stroke="var(--text-tertiary)" stroke-width="1.2"/>
				</svg>
				<p class="empty-text">No projects yet</p>
				<p class="empty-hint">Drag & drop video files here, or click Upload to get started.</p>
			{/if}
		</div>
	{:else}
		<div class="project-list">
			{#each filteredProjects as project (project.name)}
				{@const collapsed = collapsedProjects.has(project.name)}
				<div class="project-group">
					<div class="project-header" class:drop-highlight={draggingClip && dropTargetProject === project.name} role="button" tabindex="0" aria-expanded={!collapsed} onclick={() => toggleProject(project.name)} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleProject(project.name); }}} oncontextmenu={(e) => showProjectContext(e, project)} ondragover={(e) => { e.preventDefault(); dropTargetProject = project.name; }} ondragleave={() => { if (dropTargetProject === project.name) dropTargetProject = null; }} ondrop={async (e) => { e.preventDefault(); dropTargetProject = null; draggingClip = false; const clipName = e.dataTransfer?.getData('text/clip-name'); if (clipName) { try { await api.clips.move(clipName, project.name); await Promise.all([loadProjects(), refreshClips()]); } catch (err) { toast.error(err instanceof Error ? err.message : String(err)); } } }}>

						<svg class="chevron" class:collapsed width="12" height="12" viewBox="0 0 12 12" fill="none">
							<path d="M3 4.5l3 3 3-3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
						</svg>
						<span class="project-name">{project.display_name}</span>
						<span class="project-count mono">{project.clip_count} clip{project.clip_count !== 1 ? 's' : ''}</span>
						{#if project.created}
							<span class="project-date mono">{new Date(project.created).toLocaleDateString()}</span>
						{/if}
						<button
							class="project-delete"
							title="Delete project"
							onclick={(e) => { e.stopPropagation(); deleteProject(project.name, project.display_name); }}
						>
							<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
						</button>
					</div>
					{#if !collapsed}
						<div class="project-clips"
							ondragover={(e) => { e.preventDefault(); e.currentTarget.classList.add('drop-target'); }}
							ondragleave={(e) => { e.currentTarget.classList.remove('drop-target'); }}
							ondrop={async (e) => {
								e.preventDefault();
								e.currentTarget.classList.remove('drop-target');
								// Handle clip move (dragging an existing clip between projects)
								const clipName = e.dataTransfer?.getData('text/clip-name');
								if (clipName) {
									try {
										await api.clips.move(clipName, project.name);
										await Promise.all([loadProjects(), refreshClips()]);
									} catch (err) {
										toast.error(err instanceof Error ? err.message : String(err));
									}
									return;
								}
								// Handle file drop (uploading new files into this project)
								if (e.dataTransfer?.files.length) {
									handleFiles(e.dataTransfer.files);
								}
							}}
						>
							<!-- Folders inside project -->
							{#if project.folders?.length > 0}
								{#each project.folders as folder (folder.name)}
									{@const folderKey = `${project.name}:${folder.name}`}
									{@const folderCollapsed = collapsedProjects.has(folderKey)}
									<div class="folder-group">
										<div class="folder-header" role="button" tabindex="0" onclick={() => toggleProject(folderKey)}>
											<svg class="chevron" class:collapsed={folderCollapsed} width="10" height="10" viewBox="0 0 12 12" fill="none">
												<path d="M3 4.5l3 3 3-3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
											</svg>
											<svg width="14" height="14" viewBox="0 0 16 16" fill="none" style="color: var(--secondary)">
												<path d="M2 4h5l2 2h5v7H2V4z" stroke="currentColor" stroke-width="1.2" fill="none"/>
											</svg>
											<span class="folder-name">{folder.display_name}</span>
											<span class="folder-count mono">{folder.clips.length}</span>
										</div>
										{#if !folderCollapsed}
											<div class="clip-grid folder-clips">
												{#each folder.clips as clip (clip.name)}
													<div class="clip-wrap"
														class:selected={selectedClips.has(clip.name)}
														draggable="true"
														ondragstart={(e) => { e.dataTransfer?.setData('text/clip-name', clip.name); draggingClip = true; }}
														ondragend={() => { draggingClip = false; dropTargetProject = null; }}
														oncontextmenu={(e) => hasSelection ? showSelectionContext(e) : showClipContext(e, clip, project)}
													>
														<label class="clip-checkbox">
															<input type="checkbox" checked={selectedClips.has(clip.name)} onchange={() => toggleSelect(clip.name)} onclick={(e) => e.stopPropagation()} />
														</label>
														<ClipCard {clip} />
													</div>
												{/each}
											</div>
										{/if}
									</div>
								{/each}
							{/if}
							<!-- Loose clips (no folder) -->
							{#if project.clips.length === 0 && (!project.folders || project.folders.length === 0)}
								<p class="no-clips mono">Drop clips here or upload new ones</p>
							{/if}
							{#if project.clips.length > 0}
								<div class="clip-grid">
									{#each project.clips as clip (clip.name)}
										<div class="clip-wrap"
											class:selected={selectedClips.has(clip.name)}
											draggable="true"
											ondragstart={(e) => { e.dataTransfer?.setData('text/clip-name', clip.name); draggingClip = true; }}
										ondragend={() => { draggingClip = false; dropTargetProject = null; }}
											oncontextmenu={(e) => hasSelection ? showSelectionContext(e) : showClipContext(e, clip, project)}
										>
											<label class="clip-checkbox">
												<input
													type="checkbox"
													checked={selectedClips.has(clip.name)}
													onchange={() => toggleSelect(clip.name)}
													onclick={(e) => e.stopPropagation()}
													aria-label="Select {clip.name}"
												/>
											</label>
											<ClipCard {clip} />
											{#if projects.length > 1}
												<select
													class="move-select mono"
													value=""
													onchange={async (e) => {
														const target = (e.target as HTMLSelectElement).value;
														if (!target) return;
														try {
															await api.clips.move(clip.name, target);
															await Promise.all([loadProjects(), refreshClips()]);
														} catch (err) {
															toast.error(err instanceof Error ? err.message : String(err));
														}
													}}
												>
													<option value="" disabled selected>Move to...</option>
													{#each projects.filter(p => p.name !== project.name) as other}
														<option value={other.name}>{other.display_name}</option>
													{/each}
												</select>
											{/if}
										</div>
									{/each}
								</div>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>

<ContextMenu bind:visible={ctxVisible} x={ctxX} y={ctxY} items={ctxItems} />

<!-- Upload modal -->
{#if showUploadModal}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="modal-overlay" onclick={() => { if (uploadStatus === 'done' || uploadStatus === 'choose') showUploadModal = false; }}>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="modal" onclick={(e) => e.stopPropagation()}>
			{#if uploadStatus === 'choose'}
				<h2 class="modal-title">Upload {pendingFiles.length} file{pendingFiles.length !== 1 ? 's' : ''}</h2>
				<div class="project-choice">
					{#if projects.length > 0}
						<div class="choice-tabs">
							<button class="choice-tab mono" class:active={uploadProjectMode === 'existing'} onclick={() => uploadProjectMode = 'existing'}>Existing Project</button>
							<button class="choice-tab mono" class:active={uploadProjectMode === 'new'} onclick={() => uploadProjectMode = 'new'}>New Project</button>
						</div>
					{/if}
					{#if uploadProjectMode === 'existing' && projects.length > 0}
						<select class="modal-select mono" bind:value={uploadSelectedProject}>
							{#each projects as p}
								<option value={p.name}>{p.display_name} ({p.clip_count} clips)</option>
							{/each}
						</select>
					{:else}
						<label class="modal-field">
							<span class="field-label mono">PROJECT NAME</span>
							<input type="text" class="modal-input mono" bind:value={uploadProjectName} placeholder="e.g. Workshop Day 1" />
						</label>
					{/if}
				</div>
				{#if uploadProjectMode === 'existing' && uploadSelectedProject}
					<div class="folder-choice">
						<span class="field-label mono">FOLDER (optional)</span>
						<div class="choice-tabs">
							<button class="choice-tab mono" class:active={uploadFolderMode === 'none'} onclick={() => uploadFolderMode = 'none'}>None</button>
							{#if projects.find(p => p.name === uploadSelectedProject)?.folders?.length}
								<button class="choice-tab mono" class:active={uploadFolderMode === 'existing'} onclick={() => { uploadFolderMode = 'existing'; const f = projects.find(p => p.name === uploadSelectedProject)?.folders?.[0]; if (f) uploadSelectedFolder = f.name; }}>Existing</button>
							{/if}
							<button class="choice-tab mono" class:active={uploadFolderMode === 'new'} onclick={() => uploadFolderMode = 'new'}>New</button>
						</div>
						{#if uploadFolderMode === 'existing'}
							<select class="modal-select mono" bind:value={uploadSelectedFolder}>
								{#each projects.find(p => p.name === uploadSelectedProject)?.folders ?? [] as f}
									<option value={f.name}>{f.display_name} ({f.clips.length})</option>
								{/each}
							</select>
						{:else if uploadFolderMode === 'new'}
							<input type="text" class="modal-input mono" bind:value={uploadNewFolderName} placeholder="e.g. Scene 1" />
						{/if}
					</div>
				{/if}
				<div class="modal-files mono">
					{#each pendingFiles as f}
						<span class="file-name">{f.name}</span>
					{/each}
				</div>
				<div class="modal-actions">
					<button class="btn-primary mono" onclick={startUpload}>Upload</button>
					<button class="btn-secondary mono" onclick={() => showUploadModal = false}>Cancel</button>
				</div>
			{:else if uploadStatus === 'uploading'}
				<h2 class="modal-title">Uploading...</h2>
				<div class="modal-progress-list">
					{#each uploadFileProgress as fp}
						<div class="progress-item">
							<span class="file-name mono">{fp.name}</span>
							<div class="progress-bar-sm">
								<div class="progress-fill-sm" style="width: {fp.progress}%"></div>
							</div>
							<span class="progress-pct mono">{fp.done ? '✓' : `${fp.progress}%`}</span>
						</div>
					{/each}
				</div>
			{:else if uploadStatus === 'extracting'}
				<h2 class="modal-title">Extracting frames...</h2>
				<p class="modal-hint mono">Extraction jobs have been queued. You can close this and check progress on the Jobs page.</p>
				<button class="btn-secondary mono" onclick={() => showUploadModal = false}>Close</button>
			{:else}
				<h2 class="modal-title">Upload complete</h2>
				<button class="btn-primary mono" onclick={() => showUploadModal = false}>Done</button>
			{/if}
			{#if uploadError}
				<div class="modal-error mono">{uploadError}</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.page {
		padding: var(--sp-5) var(--sp-6);
		display: flex;
		flex-direction: column;
		gap: var(--sp-4);
		min-height: 100%;
		position: relative;
	}

	.page.drag-over { background: var(--accent-glow); }

	.page-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	/* Upload modal */
	.modal-overlay {
		position: fixed; inset: 0; background: rgba(0, 0, 0, 0.6); z-index: 1000;
		display: flex; align-items: center; justify-content: center; padding: var(--sp-4);
	}
	.modal {
		background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-lg);
		padding: var(--sp-5); width: 100%; max-width: 420px; display: flex; flex-direction: column; gap: var(--sp-3);
	}
	.modal-title { font-family: var(--font-sans); font-size: 18px; font-weight: 700; }
	.modal-field { display: flex; flex-direction: column; gap: 4px; }
	.field-label { font-size: 9px; letter-spacing: 0.08em; color: var(--text-tertiary); }
	.modal-input {
		padding: 8px 10px; background: var(--surface-3); border: 1px solid var(--border);
		border-radius: 6px; color: var(--text-primary); font-size: 13px; outline: none;
	}
	.modal-input:focus { border-color: var(--accent); }
	.modal-input::placeholder { color: var(--text-tertiary); }
	.project-choice { display: flex; flex-direction: column; gap: var(--sp-2); }
	.folder-choice { display: flex; flex-direction: column; gap: var(--sp-2); }
	.choice-tabs { display: flex; gap: 0; }
	.choice-tab {
		flex: 1; padding: 6px; font-size: 11px; letter-spacing: 0.04em; text-align: center;
		background: var(--surface-3); border: 1px solid var(--border); color: var(--text-tertiary);
		cursor: pointer; transition: all 0.15s;
	}
	.choice-tab:first-child { border-radius: 4px 0 0 4px; }
	.choice-tab:last-child { border-radius: 0 4px 4px 0; }
	.choice-tab.active { background: var(--surface-4); color: var(--text-primary); border-color: var(--accent); }
	.modal-select {
		padding: 8px 10px; background: var(--surface-3); border: 1px solid var(--border);
		border-radius: 6px; color: var(--text-primary); font-size: 13px; width: 100%;
	}
	.modal-files { display: flex; flex-direction: column; gap: 2px; max-height: 120px; overflow-y: auto; }
	.file-name { font-size: 11px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.modal-actions { display: flex; gap: var(--sp-2); }
	.btn-primary {
		padding: 8px 16px; font-size: 12px; font-weight: 600;
		background: var(--accent); color: #000; border: none; border-radius: var(--radius-sm);
		cursor: pointer; transition: all 0.15s;
	}
	.btn-primary:hover { background: #fff; }
	.btn-secondary {
		padding: 8px 16px; font-size: 12px;
		background: transparent; color: var(--text-secondary); border: 1px solid var(--border);
		border-radius: var(--radius-sm); cursor: pointer; transition: all 0.15s;
	}
	.btn-secondary:hover { color: var(--text-primary); border-color: var(--text-tertiary); }
	.modal-hint { font-size: 12px; color: var(--text-secondary); line-height: 1.4; }
	.modal-error {
		padding: var(--sp-2); background: rgba(255, 82, 82, 0.08); border: 1px solid rgba(255, 82, 82, 0.2);
		border-radius: 6px; font-size: 11px; color: var(--state-error);
	}
	.modal-progress-list { display: flex; flex-direction: column; gap: var(--sp-2); }
	.progress-item { display: flex; align-items: center; gap: var(--sp-2); }
	.progress-bar-sm { flex: 1; height: 4px; background: var(--surface-4); border-radius: 2px; overflow: hidden; }
	.progress-fill-sm { height: 100%; background: var(--accent); border-radius: 2px; transition: width 0.3s; }
	.progress-pct { font-size: 10px; color: var(--text-tertiary); min-width: 30px; text-align: right; }

	.filter-bar {
		display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap;
	}
	.filter-search {
		flex: 1; min-width: 180px; padding: 7px 10px; background: var(--surface-2);
		border: 1px solid var(--border); border-radius: 6px; color: var(--text-primary);
		font-size: 12px; outline: none;
	}
	.filter-search:focus { border-color: var(--accent); }
	.filter-search::placeholder { color: var(--text-tertiary); }
	.state-toggles { display: flex; gap: 2px; }
	.state-btn {
		padding: 4px 8px; font-size: 10px; letter-spacing: 0.04em;
		background: var(--surface-2); border: 1px solid var(--border); color: var(--text-tertiary);
		cursor: pointer; transition: all 0.15s;
	}
	.state-btn:first-child { border-radius: 4px 0 0 4px; }
	.state-btn:last-child { border-radius: 0 4px 4px 0; }
	.state-btn:hover { color: var(--text-secondary); }
	.state-btn.active { background: var(--surface-4); color: var(--text-primary); border-color: var(--text-tertiary); }
	.state-btn[data-state="RAW"].active { color: var(--state-raw); }
	.state-btn[data-state="READY"].active { color: var(--state-ready); }
	.state-btn[data-state="COMPLETE"].active { color: var(--state-complete); }
	.state-btn[data-state="ERROR"].active { color: var(--state-error); }
	.state-btn[data-state="EXTRACTING"].active { color: var(--state-extracting); }
	.state-btn[data-state="MASKED"].active { color: var(--state-masked); }

	.header-left {
		display: flex;
		align-items: baseline;
		gap: var(--sp-3);
	}

	.page-title {
		font-family: var(--font-sans);
		font-size: 20px;
		font-weight: 700;
		letter-spacing: -0.01em;
	}

	.header-count {
		font-size: 12px;
		color: var(--text-tertiary);
	}

	.header-actions {
		display: flex;
		gap: var(--sp-2);
		align-items: center;
	}

	.btn-accent {
		display: inline-flex;
		align-items: center;
		gap: var(--sp-2);
		padding: 6px var(--sp-3);
		font-size: 13px;
		font-weight: 600;
		color: #000;
		background: var(--accent);
		border: none;
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: all 0.15s;
	}
	.btn-accent:hover { background: #fff; box-shadow: 0 0 12px rgba(255, 242, 3, 0.25); }
	.btn-accent.disabled { opacity: 0.5; pointer-events: none; }

	.btn-ghost {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		padding: 6px var(--sp-3);
		font-size: 13px;
		font-weight: 500;
		color: var(--text-secondary);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: all 0.15s;
	}
	.btn-ghost:hover { color: var(--text-primary); border-color: var(--text-tertiary); background: var(--surface-2); }
	.btn-ghost:disabled { opacity: 0.5; cursor: not-allowed; }

	.spinning { animation: spin 1s linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }

	/* Create project form */
	.create-form {
		display: flex;
		align-items: center;
		gap: var(--sp-2);
		padding: var(--sp-3) var(--sp-4);
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
	}

	.create-input {
		flex: 1;
		padding: 6px var(--sp-3);
		font-size: 14px;
		background: var(--surface-3);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--text-primary);
		outline: none;
		font-family: inherit;
	}
	.create-input:focus { border-color: var(--accent); }
	.create-input::placeholder { color: var(--text-tertiary); }

	.btn-sm {
		padding: 6px 14px;
		font-size: 12px;
		font-weight: 600;
		background: var(--accent);
		color: #000;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
	}
	.btn-sm:hover { background: #fff; }
	.btn-sm:disabled { opacity: 0.4; cursor: not-allowed; }

	.btn-ghost-sm {
		padding: 6px 10px;
		font-size: 12px;
		color: var(--text-tertiary);
		background: none;
		border: none;
		cursor: pointer;
	}
	.btn-ghost-sm:hover { color: var(--text-primary); }

	.error-banner {
		padding: var(--sp-3) var(--sp-4);
		background: rgba(255, 82, 82, 0.06);
		border: 1px solid rgba(255, 82, 82, 0.2);
		border-radius: var(--radius-md);
		font-size: 12px;
		color: var(--state-error);
	}

	.upload-bar {
		position: relative;
		height: 28px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		margin-bottom: var(--sp-3);
		overflow: hidden;
	}

	.upload-bar-fill {
		position: absolute;
		inset: 0;
		background: var(--accent-muted);
		transition: width 0.2s ease;
	}

	.upload-bar-text {
		position: relative;
		z-index: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		height: 100%;
		font-size: 11px;
		color: var(--accent);
		letter-spacing: 0.06em;
	}

	.drop-overlay {
		position: absolute;
		inset: 0;
		z-index: 10;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.85);
		border: 2px dashed var(--accent);
		border-radius: var(--radius-lg);
		pointer-events: none;
	}

	.drop-content {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--sp-3);
	}

	.drop-text {
		font-size: 16px;
		font-weight: 600;
		color: var(--accent);
	}

	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--sp-3);
		padding: var(--sp-10) 0;
		text-align: center;
	}
	.empty-text { font-size: 16px; font-weight: 500; color: var(--text-secondary); }
	.empty-hint { font-size: 13px; color: var(--text-tertiary); max-width: 300px; }

	/* Project groups */
	.project-list {
		display: flex;
		flex-direction: column;
		gap: var(--sp-3);
	}

	.project-group {
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		overflow: hidden;
		background: var(--surface-1);
	}

	.project-header {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
		width: 100%;
		padding: var(--sp-3) var(--sp-4);
		background: var(--surface-2);
		border: none;
		color: var(--text-primary);
		cursor: pointer;
		font-family: inherit;
		font-size: 14px;
		text-align: left;
		transition: background 0.1s;
	}

	.project-header:hover {
		background: var(--surface-3);
	}
	.project-header.drop-highlight {
		background: rgba(255, 242, 3, 0.08);
		border-color: var(--accent);
		box-shadow: inset 0 0 12px rgba(255, 242, 3, 0.05);
	}

	.chevron {
		transition: transform 0.15s;
		color: var(--text-tertiary);
		flex-shrink: 0;
	}

	.chevron.collapsed {
		transform: rotate(-90deg);
	}

	.project-name {
		font-weight: 600;
		flex: 1;
	}

	.project-count {
		font-size: 11px;
		color: var(--text-secondary);
	}

	.project-date {
		font-size: 10px;
		color: var(--text-tertiary);
	}

	.project-delete {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-tertiary);
		cursor: pointer;
		transition: all 0.1s;
		flex-shrink: 0;
	}
	.project-delete:hover {
		color: var(--state-error);
		background: rgba(255, 82, 82, 0.1);
	}

	/* Folders inside projects */
	.folder-group {
		border-top: 1px solid var(--border-subtle);
	}
	.folder-header {
		display: flex; align-items: center; gap: var(--sp-2);
		padding: var(--sp-2) var(--sp-4) var(--sp-2) var(--sp-6);
		cursor: pointer; transition: background 0.15s;
		font-size: 13px;
	}
	.folder-header:hover { background: var(--surface-3); }
	.folder-name { font-weight: 500; color: var(--text-secondary); }
	.folder-count { font-size: 10px; color: var(--text-tertiary); }
	.folder-clips { padding-left: var(--sp-4); }

	.project-clips {
		padding: var(--sp-3) var(--sp-4) var(--sp-4);
	}

	.no-clips {
		font-size: 12px;
		color: var(--text-tertiary);
		padding: var(--sp-2);
	}

	.project-clips :global(.drop-target) {
		background: var(--accent-glow);
		outline: 2px dashed var(--accent);
		outline-offset: -2px;
		border-radius: var(--radius-md);
	}

	.clip-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: var(--sp-3);
	}

	.clip-wrap {
		position: relative;
		cursor: grab;
	}

	.clip-wrap.selected {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
		border-radius: var(--radius-md);
	}

	.clip-wrap:active {
		cursor: grabbing;
		opacity: 0.7;
	}

	.clip-checkbox {
		position: absolute;
		top: 6px;
		left: 6px;
		z-index: 2;
		cursor: pointer;
	}

	.clip-checkbox input {
		width: 16px;
		height: 16px;
		accent-color: var(--accent);
		cursor: pointer;
	}

	.selection-bar {
		display: flex;
		align-items: center;
		gap: var(--sp-3);
		padding: var(--sp-2) var(--sp-4);
		background: var(--accent-muted);
		border: 1px solid var(--accent);
		border-radius: var(--radius-md);
	}

	.selection-count {
		font-size: 12px;
		color: var(--accent);
		font-weight: 600;
	}

	.move-select {
		position: absolute;
		bottom: var(--sp-2);
		left: var(--sp-2);
		right: var(--sp-2);
		padding: 4px 6px;
		font-size: 10px;
		background: rgba(0, 0, 0, 0.8);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--text-secondary);
		cursor: pointer;
		opacity: 0;
		transition: opacity 0.15s;
		backdrop-filter: blur(4px);
	}

	.clip-wrap:hover .move-select {
		opacity: 1;
	}
</style>
