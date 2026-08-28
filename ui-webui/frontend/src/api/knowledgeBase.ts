import { ApiError, client, getBaseUrl, getUserId } from './client';
import type {
	CreateKnowledgeBaseRequest,
	CreateKnowledgeBaseResponse,
	KbMiddlewareParametersSchemaResponse,
	KnowledgeBaseView,
	ListKbEmbeddingModelsResponse,
	ListKnowledgeBasesResponse,
	ListKnowledgeDocumentsResponse,
	ListKnowledgeDocumentStatusResponse,
	ListSupportedContentTypesResponse,
	SearchKnowledgeBaseRequest,
	SearchKnowledgeBaseResponse,
	UpdateKnowledgeBaseRequest,
	UploadKnowledgeDocumentResponse,
} from './types';

/**
 * Callback invoked while bytes are pushed across the wire.
 *
 * - `loaded` — bytes already sent.
 * - `total` — total bytes (may be 0 when the browser cannot compute it,
 *   e.g. for chunked encodings).
 */
export interface UploadProgress {
	loaded: number;
	total: number;
}

export interface UploadDocumentOptions {
	/** Fired with byte-level progress while the body is streamed. */
	onProgress?: (progress: UploadProgress) => void;
	/**
	 * Caller-supplied abort signal. Aborting before the server has
	 * responded rejects the returned promise with a `DOMException` of
	 * `name === "AbortError"`; aborting after a response has come back
	 * is a no-op.
	 */
	signal?: AbortSignal;
}

/**
 * XHR-based upload — `fetch` does not surface byte-level send
 * progress in any current browser, so multipart uploads that drive a
 * progress UI have to fall back to XMLHttpRequest.
 */
/** Media types the KB parsers accept, keyed by extension. Only consulted when
 *  the browser could not name the type itself. */
const MEDIA_TYPE_BY_EXTENSION: Record<string, string> = {
	'.csv': 'text/csv',
	'.htm': 'text/html',
	'.html': 'text/html',
	'.json': 'application/json',
	'.markdown': 'text/markdown',
	'.md': 'text/markdown',
	'.rst': 'text/x-rst',
	'.txt': 'text/plain',
	'.xml': 'application/xml',
	'.yaml': 'application/x-yaml',
	'.yml': 'application/x-yaml',
};

function mediaTypeForFile(file: File): string | null {
	const declared = (file.type || '').trim();
	if (declared && declared !== 'application/octet-stream') return null;
	const dot = file.name.lastIndexOf('.');
	if (dot < 0) return null;
	return MEDIA_TYPE_BY_EXTENSION[file.name.slice(dot).toLowerCase()] ?? null;
}

function uploadDocumentXhr(
	knowledgeBaseId: string,
	file: File,
	options: UploadDocumentOptions = {},
): Promise<UploadKnowledgeDocumentResponse> {
	const { onProgress, signal } = options;
	const formData = new FormData();
	formData.append('file', file);
	// The server routes an upload to a parser by media type, and browsers hand
	// us application/octet-stream (or nothing) for extensions the OS has no
	// mapping for -- .md and .rst most often. The route takes an explicit
	// override for exactly this, so resolve it from the extension instead of
	// letting a correct file be rejected as an unparseable type.
	const resolved = mediaTypeForFile(file);
	if (resolved) formData.append('content_type', resolved);

	return new Promise((resolve, reject) => {
		if (signal?.aborted) {
			reject(new DOMException('Aborted', 'AbortError'));
			return;
		}

		const xhr = new XMLHttpRequest();
		const url = new URL(`/knowledge_bases/${knowledgeBaseId}/documents`, getBaseUrl());
		xhr.open('POST', url.toString(), true);
		xhr.setRequestHeader('X-User-ID', getUserId());

		const onAbort = () => xhr.abort();
		signal?.addEventListener('abort', onAbort, { once: true });

		const cleanup = () => signal?.removeEventListener('abort', onAbort);

		if (xhr.upload && onProgress) {
			xhr.upload.onprogress = (e) => {
				onProgress({
					loaded: e.loaded,
					total: e.lengthComputable ? e.total : 0,
				});
			};
		}

		xhr.onload = () => {
			cleanup();
			if (xhr.status >= 200 && xhr.status < 300) {
				try {
					resolve(JSON.parse(xhr.responseText) as UploadKnowledgeDocumentResponse);
				} catch (e) {
					reject(e);
				}
				return;
			}
			let detail = xhr.responseText || xhr.statusText;
			try {
				const json = JSON.parse(xhr.responseText) as {
					detail?: unknown;
				};
				if (typeof json.detail === 'string') detail = json.detail;
				else if (json.detail !== undefined) detail = JSON.stringify(json.detail);
			} catch {
				// keep raw text
			}
			reject(new ApiError(xhr.status, detail));
		};
		xhr.onerror = () => {
			cleanup();
			reject(new ApiError(0, 'Network error'));
		};
		xhr.onabort = () => {
			cleanup();
			reject(new DOMException('Aborted', 'AbortError'));
		};

		xhr.send(formData);
	});
}

/**
 * Client for the `/knowledge_bases` router.
 */
export const knowledgeBaseApi = {
	list: () => client.get<ListKnowledgeBasesResponse>('/knowledge_bases/'),

	listEmbeddingModels: () =>
		client.get<ListKbEmbeddingModelsResponse>('/knowledge_bases/embedding_models'),

	/** Fetch the JSON Schema describing the KB middleware's tunable params. */
	middlewareParametersSchema: () =>
		client.get<KbMiddlewareParametersSchemaResponse>(
			'/knowledge_bases/middleware/parameters_schema',
		),

	/** List the union of media types + extensions every parser accepts. */
	supportedContentTypes: () =>
		client.get<ListSupportedContentTypesResponse>('/knowledge_bases/supported_content_types'),

	create: (body: CreateKnowledgeBaseRequest) =>
		client.post<CreateKnowledgeBaseResponse>('/knowledge_bases/', body),

	update: (knowledgeBaseId: string, body: UpdateKnowledgeBaseRequest) =>
		client.patch<KnowledgeBaseView>(`/knowledge_bases/${knowledgeBaseId}`, body),

	delete: (knowledgeBaseId: string) => client.delete(`/knowledge_bases/${knowledgeBaseId}`),

	/** List every document registered against a knowledge base. */
	listDocuments: (knowledgeBaseId: string) =>
		client.get<ListKnowledgeDocumentsResponse>(`/knowledge_bases/${knowledgeBaseId}/documents`),

	/**
	 * Batch-query lifecycle status for a list of documents.
	 *
	 * Missing ids are silently omitted by the server, so the response
	 * may be shorter than the input. An empty `ids` short-circuits
	 * locally — the backend treats an empty list as a 200 with
	 * `items: []`, but skipping the round-trip is friendlier to the
	 * polling loop.
	 */
	getDocumentStatus: (knowledgeBaseId: string, ids: string[]) => {
		if (ids.length === 0) {
			return Promise.resolve<ListKnowledgeDocumentStatusResponse>({
				items: [],
			});
		}
		return client.get<ListKnowledgeDocumentStatusResponse>(
			`/knowledge_bases/${knowledgeBaseId}/documents/status`,
			{ ids: ids.join(',') },
		);
	},

	uploadDocument: (knowledgeBaseId: string, file: File, options?: UploadDocumentOptions) =>
		uploadDocumentXhr(knowledgeBaseId, file, options),

	/** Head of a document's stored bytes, decoded as text, for preview. */
	readDocument: (knowledgeBaseId: string, documentId: string) =>
		client.get<{
			document_id: string;
			filename: string;
			content_type: string | null;
			size: number;
			truncated: boolean;
			content: string;
		}>(`/knowledge_bases/${knowledgeBaseId}/documents/${documentId}/content`),

	deleteDocument: (knowledgeBaseId: string, documentId: string) =>
		client.delete(`/knowledge_bases/${knowledgeBaseId}/documents/${documentId}`),

	search: (knowledgeBaseId: string, body: SearchKnowledgeBaseRequest) =>
		client.post<SearchKnowledgeBaseResponse>(
			`/knowledge_bases/${knowledgeBaseId}/search`,
			body,
		),
};
