/** GoTrue auth client — handles login, signup, token storage, and refresh.
 *
 * Communicates directly with the GoTrue API (Supabase Auth).
 * Stores tokens and GoTrue URL in localStorage for persistence across
 * page refreshes (CRKY-63).
 */

const TOKEN_KEY = 'ck:auth_token';
const REFRESH_KEY = 'ck:refresh_token';
const USER_KEY = 'ck:auth_user';
export interface AuthUser {
	id: string;
	email: string;
	tier: string;
	org_ids: string[];
}

export interface AuthSession {
	access_token: string;
	refresh_token: string;
	expires_at: number;
	user: AuthUser;
}

/** Initialize auth — check if auth is enabled on the server. */
export async function initAuth(): Promise<{ enabled: boolean }> {
	const res = await fetch('/api/auth/status');
	const data = await res.json();
	return { enabled: data.auth_enabled };
}

/** Login with email/password via server proxy. Returns session or throws. */
export async function login(email: string, password: string): Promise<AuthSession> {
	const res = await fetch('/api/auth/login', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ email, password })
	});

	if (!res.ok) {
		const err = await res.json().catch(() => ({ msg: res.statusText }));
		throw new Error(err.error_description || err.msg || err.detail || 'Login failed');
	}

	const data = await res.json();
	const session = parseSession(data);
	storeSession(session);
	return session;
}

/** Refresh the access token via server proxy. */
export async function refreshToken(): Promise<AuthSession | null> {
	const refresh = localStorage.getItem(REFRESH_KEY);
	if (!refresh) return null;

	try {
		const res = await fetch('/api/auth/refresh', {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'X-Refresh-Token': refresh
			}
		});

		if (!res.ok) {
			clearSession();
			return null;
		}

		const data = await res.json();
		const session = parseSession(data);
		storeSession(session);
		return session;
	} catch {
		return null;
	}
}

/** Get the stored access token, refreshing if expired. */
export async function getToken(): Promise<string | null> {
	const token = localStorage.getItem(TOKEN_KEY);
	if (!token) return null;

	// Check if expired (with 60s buffer)
	const user = getStoredUser();
	if (user) {
		try {
			const payload = JSON.parse(atob(token.split('.')[1]));
			if (payload.exp && payload.exp * 1000 < Date.now() + 60000) {
				const session = await refreshToken();
				return session?.access_token ?? null;
			}
		} catch {
			// Invalid token format
		}
	}

	return token;
}

/** Get the stored user without a network call. */
export function getStoredUser(): AuthUser | null {
	const raw = localStorage.getItem(USER_KEY);
	if (!raw) return null;
	try {
		return JSON.parse(raw);
	} catch {
		return null;
	}
}

/** Logout — clear stored tokens and GoTrue URL. */
export function logout(): void {
	clearSession();
}

/** Check if a session exists (not necessarily valid). */
export function hasSession(): boolean {
	return !!localStorage.getItem(TOKEN_KEY);
}

function parseSession(data: Record<string, unknown>): AuthSession {
	const appMeta = (data.user as Record<string, unknown>)?.app_metadata as Record<string, unknown> ?? {};
	return {
		access_token: data.access_token as string,
		refresh_token: data.refresh_token as string,
		expires_at: data.expires_at as number,
		user: {
			id: (data.user as Record<string, unknown>)?.id as string ?? '',
			email: (data.user as Record<string, unknown>)?.email as string ?? '',
			tier: (appMeta.tier as string) ?? 'pending',
			org_ids: (appMeta.org_ids as string[]) ?? []
		}
	};
}

function storeSession(session: AuthSession): void {
	localStorage.setItem(TOKEN_KEY, session.access_token);
	localStorage.setItem(REFRESH_KEY, session.refresh_token);
	localStorage.setItem(USER_KEY, JSON.stringify(session.user));
}

function clearSession(): void {
	localStorage.removeItem(TOKEN_KEY);
	localStorage.removeItem(REFRESH_KEY);
	localStorage.removeItem(USER_KEY);
}
