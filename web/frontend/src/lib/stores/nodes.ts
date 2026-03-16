import { writable } from 'svelte/store';
import { api } from '$lib/api';

export interface GPUSlot {
	index: number;
	name: string;
	vram_total_gb: number;
	vram_free_gb: number;
	status: string;
	current_job_id: string | null;
}

export interface NodeSchedule {
	enabled: boolean;
	start: string;
	end: string;
	is_active_now: boolean;
}

export interface NodeInfo {
	node_id: string;
	name: string;
	host: string;
	gpus: GPUSlot[];
	gpu_name: string;
	vram_total_gb: number;
	vram_free_gb: number;
	status: string;
	current_job_id: string | null;
	last_heartbeat: number;
	capabilities: string[];
	shared_storage: string | null;
	paused: boolean;
	schedule: NodeSchedule;
	accepted_types: string[];
}

export const nodes = writable<NodeInfo[]>([]);

let refreshPending = false;

export async function refreshNodes() {
	if (refreshPending) return;
	refreshPending = true;
	try {
		const list = await api.nodes.list();
		nodes.set(list);
	} catch {
		// silently fail
	} finally {
		refreshPending = false;
	}
}

export function updateNodeFromWS(data: NodeInfo) {
	nodes.update((list) => {
		const idx = list.findIndex((n) => n.node_id === data.node_id);
		if (idx >= 0) {
			list[idx] = data;
		} else {
			list = [...list, data];
		}
		return list;
	});
}

export function removeNodeFromWS(nodeId: string) {
	nodes.update((list) => list.filter((n) => n.node_id !== nodeId));
}
