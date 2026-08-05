import { client } from './client';

export const ravenSessionModelApi = {
	/**
	 * `model` is the session's own override; `default` is `agents.defaults.model`,
	 * which is what routes the session's turns while that override is unset.
	 */
	get: (sessionKey: string) =>
		client.get<{ model: string | null; default: string | null }>(
			`/raven/sessions/${encodeURIComponent(sessionKey)}/model`,
		),
	set: (sessionKey: string, model: string | null) =>
		client.put<{ ok: boolean; model: string | null }>(
			`/raven/sessions/${encodeURIComponent(sessionKey)}/model`,
			{ model },
		),
};
