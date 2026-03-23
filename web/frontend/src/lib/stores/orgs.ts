/** Active org store — tracks which org the user is currently working in. */

import { writable } from 'svelte/store';
import { getActiveOrgId, setActiveOrgId } from '$lib/auth';

export interface OrgSummary {
	org_id: string;
	name: string;
	personal: boolean;
}

/** The user's org list (fetched from /api/orgs). */
export const userOrgs = writable<OrgSummary[]>([]);

/** The currently active org_id. */
export const activeOrgId = writable<string>(getActiveOrgId() ?? '');

/** Switch the active org. Persists to localStorage and triggers store refresh. */
export function switchOrg(orgId: string): void {
	setActiveOrgId(orgId);
	activeOrgId.set(orgId);
}

/** Load user's orgs from the API and ensure activeOrgId is valid. */
export async function loadUserOrgs(fetchFn: typeof fetch = fetch): Promise<void> {
	try {
		const token = localStorage.getItem('ck:auth_token');
		const headers: Record<string, string> = {};
		if (token) headers['Authorization'] = `Bearer ${token}`;
		const res = await fetchFn('/api/orgs', { headers });
		if (!res.ok) return;
		const data = await res.json();
		const orgs: OrgSummary[] = (data.orgs ?? []).map((o: Record<string, unknown>) => ({
			org_id: o.org_id as string,
			name: o.name as string,
			personal: o.personal as boolean,
		}));
		userOrgs.set(orgs);

		// Validate active org — if not set or no longer a member, default to personal
		const current = getActiveOrgId();
		const valid = orgs.some((o) => o.org_id === current);
		if (!valid && orgs.length > 0) {
			const personal = orgs.find((o) => o.personal) ?? orgs[0];
			switchOrg(personal.org_id);
		} else if (current) {
			activeOrgId.set(current);
		}
	} catch {
		// ignore — orgs page handles errors
	}
}
