/** Typed fetch wrappers for the CorridorKey API. */

import { getToken, refreshToken, logout } from '$lib/auth';

const BASE = '';

/** Attach auth header to a Headers/Record object. */
async function attachAuth(headers: Record<string, string>): Promise<void> {
	const token = await getToken();
	if (token) {
		headers['Authorization'] = `Bearer ${token}`;
	}
}

/** Handle 401: try one token refresh + retry, otherwise redirect to login. */
let _redirecting = false;
async function handle401(method: string, path: string, opts: RequestInit): Promise<Response | null> {
	const session = await refreshToken();
	if (!session) {
		logout();
		// Only redirect once — prevent loop from multiple concurrent 401s
		if (!_redirecting && !window.location.pathname.startsWith('/login')) {
			_redirecting = true;
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

async function uploadRequest<T>(path: string, form: FormData): Promise<T> {
	const headers: Record<string, string> = {};
	await attachAuth(headers);
	let res = await fetch(`${BASE}${path}`, { method: 'POST', headers, body: form });
	if (res.status === 401) {
		const retry = await handle401('POST', path, { method: 'POST', headers, body: form });
		if (!retry) throw new Error('Session expired');
		res = retry;
	}
	if (!res.ok) {
		const detail = await res.json().catch(() => ({ detail: res.statusText }));
		throw new Error(detail.detail || res.statusText);
	}
	return res.json();
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
	priority: number;
	shard_group: string | null;
	shard_index: number;
	shard_total: number;
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

export interface Project {
	name: string;
	display_name: string;
	path: string;
	clip_count: number;
	created: string | null;
	clips: Clip[];
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
		delete: (name: string) => request<unknown>('DELETE', `/api/projects/${encodeURIComponent(name)}`)
	},
	clips: {
		list: () => request<ClipListResponse>('GET', '/api/clips'),
		get: (name: string) => request<Clip>('GET', `/api/clips/${encodeURIComponent(name)}`),
		delete: (name: string) => request<unknown>('DELETE', `/api/clips/${encodeURIComponent(name)}`),
		move: (name: string, targetProject: string) =>
			request<unknown>('POST', `/api/clips/${encodeURIComponent(name)}/move?target_project=${encodeURIComponent(targetProject)}`)
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
		submitGVM: (clip_names: string[], gvm_mode: string = 'speed') =>
			request<Job[]>('POST', '/api/jobs/gvm', { clip_names, gvm_mode }),
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
		video: async (file: File, name?: string, autoExtract = true): Promise<{ status: string; clips: Clip[]; extract_jobs: string[] }> => {
			const form = new FormData();
			form.append('file', file);
			const qs = new URLSearchParams();
			if (name) qs.set('name', name);
			qs.set('auto_extract', String(autoExtract));
			return uploadRequest(`/api/upload/video?${qs}`, form);
		},
		frames: async (file: File, name?: string): Promise<{ status: string; clips: Clip[]; frame_count: number }> => {
			const form = new FormData();
			form.append('file', file);
			const params = name ? `?name=${encodeURIComponent(name)}` : '';
			return uploadRequest(`/api/upload/frames${params}`, form);
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
		url: (clipName: string, passName: string, frame: number) => {
			const base = `${BASE}/api/preview/${encodeURIComponent(clipName)}/${passName}/${frame}`;
			const token = localStorage.getItem('ck:auth_token');
			return token ? `${base}?token=${encodeURIComponent(token)}` : base;
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
			request<unknown>('PUT', `/api/farm/${encodeURIComponent(nodeId)}/visibility`, { visibility })
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
