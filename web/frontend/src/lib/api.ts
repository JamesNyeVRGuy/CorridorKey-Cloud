/** Typed fetch wrappers for the CorridorKey API. */

import { getToken, refreshToken, logout, getActiveOrgId } from '$lib/auth';

const BASE = '';

/** Attach auth + org headers to a request. */
async function attachAuth(headers: Record<string, string>): Promise<void> {
	const token = await getToken();
	if (token) {
		headers['Authorization'] = `Bearer ${token}`;
	}
	const orgId = getActiveOrgId();
	if (orgId) {
		headers['X-Org-Id'] = orgId;
	}
}

/** Handle 401: try one token refresh + retry, otherwise redirect to login. */
let _redirecting = false;
async function handle401(method: string, path: string, opts: RequestInit): Promise<Response | null> {
	const session = await refreshToken();
	if (!session) {
		logout();
		// Only redirect once — prevent loop from multiple concurrent 401s.
		// Reset after 5s so future 401s aren't permanently suppressed.
		if (!_redirecting && !window.location.pathname.startsWith('/login')) {
			_redirecting = true;
			setTimeout(() => { _redirecting = false; }, 5000);
			window.location.href = '/login';
		}
		return null;
	}
	// Retry with new token
	const retryHeaders = { ...(opts.headers as Record<string, string>) };
	retryHeaders['Authorization'] = `Bearer ${session.access_token}`;
	return fetch(`${BASE}${path}`, { ...opts, headers: retryHeaders });
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
	const headers: Record<string, string> = { 'Content-Type': 'application/json' };
	await attachAuth(headers);

	const opts: RequestInit = { method, headers };
	if (body !== undefined) {
		opts.body = JSON.stringify(body);
	}
	let res = await fetch(`${BASE}${path}`, opts);

	// On 401, try refreshing the token once
	if (res.status === 401) {
		const retry = await handle401(method, path, opts);
		if (!retry) throw new Error('Session expired');
		res = retry;
	}

	if (!res.ok) {
		const detail = await res.json().catch(() => ({ detail: res.statusText }));
		throw new Error(detail.detail || res.statusText);
	}
	return res.json();
}

/** Upload progress callback — called with (loaded, total) bytes */
export type UploadProgressFn = (loaded: number, total: number) => void;

async function uploadRequest<T>(path: string, form: FormData, onProgress?: UploadProgressFn): Promise<T> {
	const headers: Record<string, string> = {};
	await attachAuth(headers);

	// Use XMLHttpRequest for progress tracking
	return new Promise<T>((resolve, reject) => {
		const xhr = new XMLHttpRequest();
		xhr.open('POST', `${BASE}${path}`);
		for (const [k, v] of Object.entries(headers)) {
			xhr.setRequestHeader(k, v);
		}
		if (onProgress) {
			xhr.upload.onprogress = (e) => {
				if (e.lengthComputable) onProgress(e.loaded, e.total);
			};
		}
		xhr.onload = () => {
			if (xhr.status === 413) {
				try {
					const body = JSON.parse(xhr.responseText);
					if (body.detail) {
						reject(new Error(body.detail));
						return;
					}
				} catch {
					// fall through to generic message
				}
				reject(new Error('File too large. The server or CDN may limit upload size (Cloudflare free: 100MB). Try compressing your video or using a ZIP of frames.'));
				return;
			}
			if (xhr.status === 401) {
				reject(new Error('Session expired'));
				return;
			}
			if (xhr.status >= 400) {
				try {
					const detail = JSON.parse(xhr.responseText);
					reject(new Error(detail.detail || xhr.statusText));
				} catch {
					reject(new Error(xhr.statusText));
				}
				return;
			}
			try {
				resolve(JSON.parse(xhr.responseText));
			} catch {
				reject(new Error('Invalid response'));
			}
		};
		xhr.onerror = () => reject(new Error('Upload failed — network error'));
		xhr.ontimeout = () => reject(new Error('Upload timed out'));
		xhr.timeout = 600000; // 10 minute timeout
		xhr.send(form);
	});
}

// --- Types ---

export interface ClipAsset {
	path: string;
	asset_type: string;
	frame_count: number;
}

export interface Clip {
	name: string;
	root_path: string;
	state: string;
	input_asset: ClipAsset | null;
	alpha_asset: ClipAsset | null;
	mask_asset: ClipAsset | null;
	frame_count: number;
	completed_frames: number;
	has_outputs: boolean;
	warnings: string[];
	error_message: string | null;
	folder_name: string | null;
	project_name: string | null;
}

export interface ClipListResponse {
	clips: Clip[];
	clips_dir: string;
}

export interface Job {
	id: string;
	job_type: string;
	clip_name: string;
	status: string;
	current_frame: number;
	total_frames: number;
	error_message: string | null;
	claimed_by: string | null;
	started_at: number;
	completed_at: number;
	duration_seconds: number;
	fps: number;
	priority: number;
	shard_group: string | null;
	shard_index: number;
	shard_total: number;
	queue_position: number | null;
	estimated_wait_seconds: number | null;
	upload_pass?: string | null;
}

export interface JobListResponse {
	current: Job | null;
	running: Job[];
	queued: Job[];
	history: Job[];
}

export interface InferenceParams {
	input_is_linear: boolean;
	despill_strength: number;
	auto_despeckle: boolean;
	despeckle_size: number;
	refiner_scale: number;
}

export interface OutputConfig {
	fg_enabled: boolean;
	fg_format: string;
	matte_enabled: boolean;
	matte_format: string;
	comp_enabled: boolean;
	comp_format: string;
	processed_enabled: boolean;
	processed_format: string;
}

export interface VRAMInfo {
	total: number;
	reserved: number;
	allocated: number;
	free: number;
	name: string;
	available: boolean;
}

export interface Folder {
	name: string;
	display_name: string;
	clips: Clip[];
}

export interface Project {
	name: string;
	display_name: string;
	path: string;
	clip_count: number;
	created: string | null;
	clips: Clip[];       // loose clips (no folder)
	folders: Folder[];   // named sub-groupings
}

export interface DeviceInfo {
	device: string;
}

export interface WeightInfo {
	installed: boolean;
	path: string;
	detail: string | null;
	size_hint: string;
	download?: { status: string; error: string | null };
}

// --- API calls ---

export const api = {
	projects: {
		list: () => request<Project[]>('GET', '/api/projects'),
		create: (name: string) => request<Project>('POST', '/api/projects', { name }),
		rename: (name: string, display_name: string) =>
			request<unknown>('PATCH', `/api/projects/${encodeURIComponent(name)}`, { display_name }),
		delete: (name: string) => request<unknown>('DELETE', `/api/projects/${encodeURIComponent(name)}`),
		createFolder: (projectName: string, folderName: string) =>
			request<Folder>('POST', `/api/projects/${encodeURIComponent(projectName)}/folders`, { name: folderName }),
		deleteFolder: (projectName: string, folderName: string) =>
			request<unknown>('DELETE', `/api/projects/${encodeURIComponent(projectName)}/folders/${encodeURIComponent(folderName)}`),
	},
	clips: {
		list: () => request<ClipListResponse>('GET', '/api/clips'),
		get: (name: string) => request<Clip>('GET', `/api/clips/${encodeURIComponent(name)}`),
		delete: (name: string) => request<unknown>('DELETE', `/api/clips/${encodeURIComponent(name)}`),
		move: (name: string, targetProject: string, targetFolder?: string) => {
			const qs = new URLSearchParams({ target_project: targetProject });
			if (targetFolder) qs.set('target_folder', targetFolder);
			return request<unknown>('POST', `/api/clips/${encodeURIComponent(name)}/move?${qs}`);
		}
	},
	jobs: {
		list: () => request<JobListResponse>('GET', '/api/jobs'),
		submitInference: (
			clip_names: string[],
			params?: Partial<InferenceParams>,
			output_config?: Partial<OutputConfig>,
			frame_range?: [number, number] | null
		) =>
			request<Job[]>('POST', '/api/jobs/inference', {
				clip_names,
				params: params ?? {},
				output_config: output_config ?? {},
				frame_range: frame_range ?? null
			}),
		submitShardedInference: (
			clip_names: string[],
			params?: Partial<InferenceParams>,
			output_config?: Partial<OutputConfig>,
			num_shards = 0
		) =>
			request<Job[]>('POST', '/api/jobs/inference/sharded', {
				clip_names,
				params: params ?? {},
				output_config: output_config ?? {},
				num_shards
			}),
		submitPipeline: (
			clip_names: string[],
			alpha_method = 'gvm',
			params?: Partial<InferenceParams>,
			output_config?: Partial<OutputConfig>
		) =>
			request<Job[]>('POST', '/api/jobs/pipeline', {
				clip_names,
				alpha_method,
				params: params ?? {},
				output_config: output_config ?? {}
			}),
		submitExtract: (clip_names: string[]) =>
			request<Job[]>('POST', '/api/jobs/extract', { clip_names }),
		submitGVM: (clip_names: string[]) =>
			request<Job[]>('POST', '/api/jobs/gvm', { clip_names }),
		submitVideoMaMa: (clip_names: string[], chunk_size = 50) =>
			request<Job[]>('POST', '/api/jobs/videomama', { clip_names, chunk_size }),
		estimate: (jobType: string, frameCount: number, numShards = 1) =>
			request<{ estimated_gpu_minutes: number; estimated_wall_clock_seconds: number; avg_seconds_per_frame: number; based_on_history: number }>(
				'GET', `/api/jobs/estimate?job_type=${jobType}&frame_count=${frameCount}&num_shards=${numShards}`),
		getLog: (jobId: string) => request<Record<string, unknown>>('GET', `/api/jobs/${jobId}/log`),
		cancel: (jobId: string) => request<unknown>('DELETE', `/api/jobs/${jobId}`),
		cancelAll: () => request<unknown>('DELETE', '/api/jobs'),
		move: (jobId: string, position: number) =>
			request<unknown>('POST', `/api/jobs/${jobId}/move?position=${position}`),
		setPriority: (jobId: string, priority: number) =>
			request<unknown>('POST', `/api/jobs/${jobId}/priority?priority=${priority}`),
		shardGroupProgress: (groupId: string) =>
			request<{ shard_group: string; total_shards: number; completed: number; running: number; failed: number; current_frame: number; total_frames: number }>('GET', `/api/jobs/shard-group/${groupId}`),
		cancelShardGroup: (groupId: string) =>
			request<unknown>('DELETE', `/api/jobs/shard-group/${groupId}`),
		retryShardGroup: (groupId: string) =>
			request<unknown>('POST', `/api/jobs/shard-group/${groupId}/retry`)
	},
	system: {
		device: () => request<DeviceInfo>('GET', '/api/system/device'),
		vram: () => request<VRAMInfo>('GET', '/api/system/vram'),
		unload: () => request<unknown>('POST', '/api/system/unload'),
		getVramLimit: () => request<{ vram_limit_gb: number }>('GET', '/api/system/vram-limit'),
		setVramLimit: (gb: number) => request<unknown>('POST', `/api/system/vram-limit?vram_limit_gb=${gb}`),
		weights: () => request<Record<string, WeightInfo>>('GET', '/api/system/weights'),
		downloadWeights: (name: string) => request<unknown>('POST', `/api/system/weights/download/${name}`)
	},
	upload: {
		video: async (file: File, name?: string, autoExtract = true, onProgress?: UploadProgressFn, project?: string, folder?: string): Promise<{ status: string; clips: Clip[]; extract_jobs: string[] }> => {
			const form = new FormData();
			form.append('file', file);
			const qs = new URLSearchParams();
			if (name) qs.set('name', name);
			if (project) qs.set('project', project);
			if (folder) qs.set('folder', folder);
			qs.set('auto_extract', String(autoExtract));
			return uploadRequest(`/api/upload/video?${qs}`, form, onProgress);
		},
		frames: async (file: File, name?: string, onProgress?: UploadProgressFn): Promise<{ status: string; clips: Clip[]; frame_count: number }> => {
			const form = new FormData();
			form.append('file', file);
			const params = name ? `?name=${encodeURIComponent(name)}` : '';
			return uploadRequest(`/api/upload/frames${params}`, form, onProgress);
		},
		images: async (files: File[], project?: string, onProgress?: UploadProgressFn, folder?: string): Promise<{ status: string; clips: Clip[]; frame_count: number }> => {
			const form = new FormData();
			for (const f of files) form.append('files', f);
			const qs = new URLSearchParams();
			if (project) qs.set('project', project);
			if (folder) qs.set('folder', folder);
			const params = qs.toString() ? `?${qs}` : '';
			return uploadRequest(`/api/upload/images${params}`, form, onProgress);
		},
		mask: async (clipName: string, file: File): Promise<unknown> => {
			const form = new FormData();
			form.append('file', file);
			return uploadRequest(`/api/upload/mask/${encodeURIComponent(clipName)}`, form);
		},
		alpha: async (clipName: string, file: File): Promise<unknown> => {
			const form = new FormData();
			form.append('file', file);
			return uploadRequest(`/api/upload/alpha/${encodeURIComponent(clipName)}`, form);
		}
	},
	preview: {
		url: (clipName: string, passName: string, frame: number, width?: number) => {
			const base = `${BASE}/api/preview/${encodeURIComponent(clipName)}/${passName}/${frame}`;
			const params = new URLSearchParams();
			const token = localStorage.getItem('ck:auth_token');
			if (token) params.set('token', token);
			const orgId = getActiveOrgId();
			if (orgId) params.set('org', orgId);
			if (width) params.set('width', String(width));
			const qs = params.toString();
			return qs ? `${base}?${qs}` : base;
		}
	},
	nodes: {
		list: () => request<(import('$lib/stores/nodes').NodeInfo & { can_manage?: boolean })[]>('GET', '/api/farm'),
		remove: (nodeId: string) => request<unknown>('DELETE', `/api/farm/${encodeURIComponent(nodeId)}`),
		pause: (nodeId: string) => request<unknown>('POST', `/api/farm/${encodeURIComponent(nodeId)}/pause`),
		resume: (nodeId: string) => request<unknown>('POST', `/api/farm/${encodeURIComponent(nodeId)}/resume`),
		setSchedule: (nodeId: string, schedule: { enabled: boolean; start: string; end: string }) =>
			request<unknown>('PUT', `/api/farm/${encodeURIComponent(nodeId)}/schedule`, schedule),
		setAcceptedTypes: (nodeId: string, types: string[]) =>
			request<unknown>('PUT', `/api/farm/${encodeURIComponent(nodeId)}/accepted-types`, {
				accepted_types: types
			}),
		getLogs: (nodeId: string) =>
			request<{ logs: string[] }>('GET', `/api/farm/${encodeURIComponent(nodeId)}/logs`),
		getHealth: (nodeId: string) =>
			request<{ history: { ts: number; cpu: number; ram_used: number; ram_total: number }[] }>('GET', `/api/farm/${encodeURIComponent(nodeId)}/health`),
		setVisibility: (nodeId: string, visibility: 'private' | 'shared') =>
			request<unknown>('PUT', `/api/farm/${encodeURIComponent(nodeId)}/visibility`, { visibility }),
		getMetrics: (nodeId: string) =>
			request<{
				node_id: string;
				reputation: {
					score: number;
					breakdown: {
						success: { value: number; weight: number; points: number };
						speed: { value: number; weight: number; points: number };
						uptime: { value: number; weight: number; points: number };
						transfer: { download_mbps: number; upload_mbps: number; combined_mbps: number; weight: number; points: number };
						security_penalty: { warnings: number; points: number };
					};
					stats: {
						completed_jobs: number;
						failed_jobs: number;
						total_frames: number;
						total_processing_seconds: number;
						total_heartbeats: number;
						missed_heartbeats: number;
					};
				};
				jobs: {
					job_id: string;
					job_type: string;
					status: string;
					total_frames: number;
					duration_seconds: number;
					fps: number;
					started_at: number;
					completed_at: number;
					clip_name?: string;
				}[];
			}>('GET', `/api/farm/${encodeURIComponent(nodeId)}/metrics`)
	},
	system2: {
		localGpus: () =>
			request<{ index: number; name: string; vram_total_gb: number; vram_free_gb: number }[]>(
				'GET',
				'/api/system/gpus'
			),
		localCpu: () =>
			request<{ cpu_percent: number; cpu_count: number; ram_total_gb: number; ram_used_gb: number; ram_free_gb: number }>(
				'GET',
				'/api/system/cpu'
			),
		getLocalGpu: () => request<{ enabled: boolean }>('GET', '/api/system/local-gpu'),
		setLocalGpu: (enabled: boolean) =>
			request<unknown>('POST', `/api/system/local-gpu?enabled=${enabled}`),
		getClaimDelay: () => request<{ seconds: number }>('GET', '/api/system/claim-delay'),
		setClaimDelay: (seconds: number) =>
			request<unknown>('POST', `/api/system/claim-delay?seconds=${seconds}`)
	}
};
