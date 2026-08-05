import { toast } from 'sonner';

const envServerUrl = (import.meta.env.VITE_SERVER_URL ?? '').trim();
const envServicePort = (import.meta.env.VITE_SERVICE_PORT ?? '').trim() || '8000';

/**
 * The one identity this deployment ever uses. Raven's web app is single-user, but
 * the agent service still namespaces its Redis records per user and rejects a
 * request with no `X-User-ID` (401), so we send this constant rather than nothing.
 *
 * The value is a storage namespace: changing it does not delete anything, it
 * addresses a different — and initially empty — set of sessions, credentials and
 * knowledge bases.
 */
export const USER_ID = 'demo';

/**
 * Where the agent service lives when nothing is stored locally. `VITE_SERVER_URL`
 * pins it outright; otherwise it is the port the service listens on (kept in sync
 * with `start_webapp.sh` via `VITE_SERVICE_PORT`) on the host serving the UI — so
 * plain localhost and a forwarded remote host both resolve without being asked.
 */
export const defaultBaseUrl = (): string => {
	if (envServerUrl) return envServerUrl;
	const { protocol, hostname } = window.location;
	return `${protocol}//${hostname}:${envServicePort}`;
};

export const getBaseUrl = () => localStorage.getItem('server_url')?.trim() || defaultBaseUrl();
export const getUserId = () => USER_ID;

/**
 * Resolve an API path against the configured server. `server_url` is free text
 * from the connection settings, so anything that addresses the backend has to
 * normalise the same way — a second mechanism (string concatenation) diverges on
 * a trailing slash and produces a URL that 404s.
 */
export const apiUrl = (path: string): URL => new URL(path, getBaseUrl());

/**
 * Structured error thrown for non-2xx HTTP responses.
 * `message` contains the human-readable detail extracted from the backend.
 */
export class ApiError extends Error {
	readonly status: number;
	readonly detail: string;

	constructor(status: number, detail: string) {
		super(detail);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
	}
}

interface RequestOptions {
	method?: string;
	body?: unknown;
	params?: Record<string, string>;
	/** When true, suppresses the automatic error toast. Useful when the caller shows its own inline error UI. */
	silent?: boolean;
}

function buildHeaders(hasBody: boolean): Record<string, string> {
	const headers: Record<string, string> = { 'X-User-ID': getUserId() };
	if (hasBody) headers['Content-Type'] = 'application/json';
	return headers;
}

/** Parse the response body and extract the `detail` field if the backend returned JSON. */
async function extractErrorDetail(res: Response): Promise<string> {
	const text = await res.text();
	try {
		const json = JSON.parse(text) as { detail?: unknown };
		if (typeof json.detail === 'string') return json.detail;
		if (json.detail !== undefined) return JSON.stringify(json.detail);
	} catch {
		// not JSON – fall through
	}
	return text || res.statusText;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
	const { method = 'GET', body, params, silent = false } = options;
	const url = apiUrl(path);
	if (params) {
		Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
	}

	const res = await fetch(url.toString(), {
		method,
		headers: buildHeaders(body !== undefined),
		body: body ? JSON.stringify(body) : undefined,
	});

	if (!res.ok) {
		const detail = await extractErrorDetail(res);
		const error = new ApiError(res.status, detail);
		if (!silent) toast.error(detail);
		throw error;
	}

	if (res.status === 204) return undefined as T;
	return res.json() as Promise<T>;
}

async function streamRequest(
	path: string,
	options: RequestOptions & { signal?: AbortSignal } = {},
): Promise<Response> {
	const { method = 'GET', body, params, signal, silent = false } = options;
	const url = apiUrl(path);
	if (params) {
		Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
	}

	const res = await fetch(url.toString(), {
		method,
		headers: buildHeaders(body !== undefined),
		body: body ? JSON.stringify(body) : undefined,
		signal,
	});

	if (!res.ok) {
		const detail = await extractErrorDetail(res);
		const error = new ApiError(res.status, detail);
		if (!silent) toast.error(detail);
		throw error;
	}

	return res;
}

export const client = {
	get: <T>(path: string, params?: Record<string, string>, options?: { silent?: boolean }) =>
		request<T>(path, { method: 'GET', params, silent: options?.silent }),
	post: <T>(
		path: string,
		body?: unknown,
		params?: Record<string, string>,
		options?: { silent?: boolean },
	) => request<T>(path, { method: 'POST', body, params, silent: options?.silent }),
	patch: <T>(
		path: string,
		body?: unknown,
		params?: Record<string, string>,
		options?: { silent?: boolean },
	) => request<T>(path, { method: 'PATCH', body, params, silent: options?.silent }),
	put: <T>(
		path: string,
		body?: unknown,
		params?: Record<string, string>,
		options?: { silent?: boolean },
	) => request<T>(path, { method: 'PUT', body, params, silent: options?.silent }),
	delete: <T = void>(path: string, params?: Record<string, string>) =>
		request<T>(path, { method: 'DELETE', params }),
	stream: (path: string, options?: RequestOptions & { signal?: AbortSignal }) =>
		streamRequest(path, options),
};
