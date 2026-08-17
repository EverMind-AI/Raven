import { client } from './client';

export const ravenSessionWorkdirApi = {
	/**
	 * `workdir` is the session's own override; `default` is where the session
	 * works while that override is unset.
	 */
	get: (sessionKey: string) =>
		client.get<{ workdir: string | null; default: string }>(
			`/raven/sessions/${encodeURIComponent(sessionKey)}/workdir`,
		),
	set: (sessionKey: string, workdir: string | null) =>
		client.put<{ ok: boolean; workdir: string | null }>(
			`/raven/sessions/${encodeURIComponent(sessionKey)}/workdir`,
			{ workdir },
		),
};
