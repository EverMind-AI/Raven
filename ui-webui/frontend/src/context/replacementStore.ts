/**
 * Which document each replacement is meant to retire, kept across a reload.
 *
 * Replacing a document is two steps -- upload the new file, then delete the old
 * record once the new one indexes -- and only the first happens on the server.
 * The pairing that joins them used to live in the documents panel's state, then
 * on the upload task; both die with the page. `UploadProvider` holds its tasks in
 * a plain reducer with no persistence, and the `File` payloads sit in a
 * non-serialisable `Map`, so a reload starts from an empty task list. The
 * replacement still reaches `ready` server-side, and the original is never
 * retired: the base keeps both versions and search returns stale chunks
 * alongside the new ones, with nothing on screen saying so.
 *
 * The `beforeunload` guard does not cover it either, deliberately -- it fires
 * only for `queued`/`uploading`, because a document already in `parsing` or
 * `indexing` finishes without the browser. True for a plain upload; for a
 * replacement the half that needs the browser is the delete, which is exactly
 * what the reload discards.
 *
 * So the pairing is written down where it can outlive the page. Keyed by the
 * *new* document id rather than the task id, because after a reload there is no
 * task to look up: what the panel has is the document list it just fetched, and
 * the new document's id is what it can find in there.
 *
 * `sessionStorage` rather than `localStorage`: this is per-tab in-flight work.
 * A pairing that outlives the tab describes a replacement nobody is watching, and
 * on the next visit the documents list is the truth.
 */

const KEY = 'raven.kb.pendingReplacements';

export interface PendingReplacement {
	knowledgeBaseId: string;
	/** The document to delete once `newDocumentId` reaches `ready`. */
	replacesDocumentId: string;
}

type Store = Record<string, PendingReplacement>;

function read(): Store {
	try {
		const raw = sessionStorage.getItem(KEY);
		if (!raw) return {};
		const parsed: unknown = JSON.parse(raw);
		// Anything other than an object means someone else wrote the key, or a
		// previous version used a different shape. Start over rather than throw
		// on every call for the rest of the session.
		if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
		return parsed as Store;
	} catch {
		// Private-mode Safari throws on sessionStorage access, and a malformed
		// value throws in JSON.parse. Neither is worth failing an upload over --
		// without persistence the feature degrades to what it did before.
		return {};
	}
}

function write(store: Store): void {
	try {
		sessionStorage.setItem(KEY, JSON.stringify(store));
	} catch {
		// Quota or private mode. Same reasoning as read().
	}
}

/** Record that `newDocumentId` is standing in for `replacesDocumentId`. */
export function rememberReplacement(
	newDocumentId: string,
	entry: PendingReplacement,
): void {
	write({ ...read(), [newDocumentId]: entry });
}

/** Drop the pairing for `newDocumentId`, once acted on or no longer wanted. */
export function forgetReplacement(newDocumentId: string): void {
	const store = read();
	if (!(newDocumentId in store)) return;
	delete store[newDocumentId];
	write(store);
}

/** Every pairing recorded for one knowledge base, keyed by new document id. */
export function pendingReplacements(knowledgeBaseId: string): Store {
	// Each value is validated, not just the container: `read()` only proves the
	// top level is an object, so a `{"someId": null}` written by anything else --
	// or by an earlier shape of this store -- would otherwise raise a TypeError
	// out of whoever called this rather than being discarded.
	return Object.fromEntries(
		Object.entries(read()).filter(
			([, e]) =>
				typeof e?.knowledgeBaseId === 'string' &&
				typeof e?.replacesDocumentId === 'string' &&
				e.knowledgeBaseId === knowledgeBaseId,
		),
	);
}
