import type {
	ApiErrorResponse,
	AskRequest,
	AskResponse,
	ChatHistoryResponse,
	ClearSessionResponse,
	DocumentImportRequest,
	DocumentRecord,
	EvidenceDetail,
	FastApiDetailError,
	FastApiValidationError,
	HealthResponse,
	ImportResult,
	KnowledgeBase,
	KnowledgeBaseCreateRequest,
	KnowledgeBaseCreateResponse,
	Project,
	ProjectCreateRequest,
	ProjectCreateResponse,
	RetrieveRequest,
	RetrieveResponse,
	ScopePayload,
	UploadDocumentRequest,
	ValidationIssue
} from './types';

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
	baseUrl?: string;
	fetch?: FetchLike;
}

export interface ApiRequestOptions {
	signal?: AbortSignal;
}

export interface SessionAccess extends ScopePayload {
	session_token: string;
}

export class ApiError extends Error {
	readonly status: number;
	readonly statusText: string;
	readonly body: unknown;

	constructor(
		message: string,
		options: { status: number; statusText?: string; body?: unknown; cause?: unknown }
	) {
		super(message, { cause: options.cause });
		this.name = 'ApiError';
		this.status = options.status;
		this.statusText = options.statusText ?? '';
		this.body = options.body;
	}
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isValidationIssue(value: unknown): value is ValidationIssue {
	return (
		isRecord(value) &&
		typeof value.type === 'string' &&
		Array.isArray(value.loc) &&
		value.loc.every((part) => typeof part === 'string' || typeof part === 'number') &&
		typeof value.msg === 'string'
	);
}

function isDetailError(value: unknown): value is FastApiDetailError {
	return isRecord(value) && typeof value.detail === 'string';
}

function isValidationError(value: unknown): value is FastApiValidationError {
	return isRecord(value) && Array.isArray(value.detail) && value.detail.every(isValidationIssue);
}

function validationMessage(error: FastApiValidationError): string {
	return error.detail.map((issue) => `${issue.loc.join('.')}: ${issue.msg}`).join('; ');
}

function parseResponseBody(text: string): unknown {
	if (!text) return undefined;

	try {
		return JSON.parse(text) as unknown;
	} catch {
		return text;
	}
}

function errorMessage(body: unknown, response: Response): string {
	if (isDetailError(body)) return body.detail;
	if (isValidationError(body)) return validationMessage(body);
	if (typeof body === 'string' && body.trim()) return body.trim();
	return response.statusText || `Request failed with status ${response.status}`;
}

export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
	const body = parseResponseBody(await response.text());
	return new ApiError(errorMessage(body, response), {
		status: response.status,
		statusText: response.statusText,
		body
	});
}

function joinUrl(baseUrl: string, path: string): string {
	return `${baseUrl.replace(/\/$/, '')}${path}`;
}

function scopeSearchParams(scope: SessionAccess): URLSearchParams {
	return new URLSearchParams({
		kb_id: scope.kb_id,
		project_id: scope.project_id,
		department: scope.department
	});
}

interface RequestTransport {
	request<T>(path: string, init?: RequestInit): Promise<T>;
	jsonRequest<T>(path: string, payload: object, requestOptions?: ApiRequestOptions): Promise<T>;
}

function createTransport(options: ApiClientOptions): RequestTransport {
	const baseUrl = options.baseUrl ?? '';
	const fetchResponse = options.fetch ?? ((input, init) => globalThis.fetch(input, init));

	async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
		let response: Response;
		try {
			response = await fetchResponse(joinUrl(baseUrl, path), init);
		} catch (cause) {
			if (cause instanceof ApiError) throw cause;
			if (cause instanceof Error && cause.name === 'AbortError') throw cause;
			throw new ApiError(cause instanceof Error ? cause.message : 'Network request failed', {
				status: 0,
				body: undefined,
				cause
			});
		}

		if (!response.ok) throw await apiErrorFromResponse(response);

		const text = await response.text();
		if (!text) return undefined as T;

		const body = parseResponseBody(text);
		if (typeof body === 'string') {
			throw new ApiError('Expected a JSON response from the API', {
				status: response.status,
				statusText: response.statusText,
				body
			});
		}
		return body as T;
	}

	function jsonRequest<T>(
		path: string,
		payload: object,
		requestOptions?: ApiRequestOptions
	): Promise<T> {
		return request<T>(path, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(payload),
			signal: requestOptions?.signal
		});
	}

	return { request, jsonRequest };
}

export interface RagApiClient {
	health(options?: ApiRequestOptions): Promise<HealthResponse>;
	listKnowledgeBases(options?: ApiRequestOptions): Promise<KnowledgeBase[]>;
	createKnowledgeBase(
		payload: KnowledgeBaseCreateRequest,
		options?: ApiRequestOptions
	): Promise<KnowledgeBaseCreateResponse>;
	listProjects(kbId: string, options?: ApiRequestOptions): Promise<Project[]>;
	createProject(
		payload: ProjectCreateRequest,
		options?: ApiRequestOptions
	): Promise<ProjectCreateResponse>;
	listDocuments(kbId: string, options?: ApiRequestOptions): Promise<DocumentRecord[]>;
	uploadDocument(
		file: File,
		payload: UploadDocumentRequest,
		options?: ApiRequestOptions
	): Promise<ImportResult>;
	importDocument(
		payload: DocumentImportRequest,
		options?: ApiRequestOptions
	): Promise<ImportResult>;
	ask(payload: AskRequest, options?: ApiRequestOptions): Promise<AskResponse>;
	retrieve(payload: RetrieveRequest, options?: ApiRequestOptions): Promise<RetrieveResponse>;
	getEvidence(chunkId: string, options?: ApiRequestOptions): Promise<EvidenceDetail>;
}

export interface SessionApiClient {
	getHistory(
		sessionId: string,
		scope: SessionAccess,
		options?: ApiRequestOptions
	): Promise<ChatHistoryResponse>;
	clearSession(
		sessionId: string,
		scope: SessionAccess,
		options?: ApiRequestOptions
	): Promise<ClearSessionResponse>;
}

export function createRagClient(options: ApiClientOptions = {}): RagApiClient {
	const { request, jsonRequest } = createTransport(options);

	return {
		health: (requestOptions) => request('/api/health', { signal: requestOptions?.signal }),
		listKnowledgeBases: (requestOptions) =>
			request('/api/knowledge-bases', { signal: requestOptions?.signal }),
		createKnowledgeBase: (payload, requestOptions) =>
			jsonRequest('/api/knowledge-bases', payload, requestOptions),
		listProjects: (kbId, requestOptions) =>
			request(`/api/projects?${new URLSearchParams({ kb_id: kbId })}`, {
				signal: requestOptions?.signal
			}),
		createProject: (payload, requestOptions) =>
			jsonRequest('/api/projects', payload, requestOptions),
		listDocuments: (kbId, requestOptions) =>
			request(`/api/documents?${new URLSearchParams({ kb_id: kbId })}`, {
				signal: requestOptions?.signal
			}),
		uploadDocument: (file, payload, requestOptions) => {
			const form = new FormData();
			form.set('file', file);
			form.set('kb_id', payload.kb_id);
			form.set('project_id', payload.project_id);
			form.set('department', payload.department);
			form.set('chunking_strategy', payload.chunking_strategy ?? 'recursive');
			return request('/api/documents/upload', {
				method: 'POST',
				body: form,
				signal: requestOptions?.signal
			});
		},
		importDocument: (payload, requestOptions) =>
			jsonRequest('/api/v1/document/import', payload, requestOptions),
		ask: (payload, requestOptions) => jsonRequest('/api/ask', payload, requestOptions),
		retrieve: (payload, requestOptions) => jsonRequest('/api/retrieve', payload, requestOptions),
		getEvidence: (chunkId, requestOptions) =>
			request(`/api/evidence/${encodeURIComponent(chunkId)}`, { signal: requestOptions?.signal })
	};
}

export function createSessionClient(options: ApiClientOptions = {}): SessionApiClient {
	const { request } = createTransport(options);

	return {
		getHistory: (sessionId, scope, requestOptions) =>
			request(`/api/v1/chat/history/${encodeURIComponent(sessionId)}?${scopeSearchParams(scope)}`, {
				headers: { 'x-session-token': scope.session_token },
				signal: requestOptions?.signal
			}),
		clearSession: (sessionId, scope, requestOptions) =>
			request(`/api/v1/chat/session/${encodeURIComponent(sessionId)}?${scopeSearchParams(scope)}`, {
				method: 'DELETE',
				headers: { 'x-session-token': scope.session_token },
				signal: requestOptions?.signal
			})
	};
}

export const rag = createRagClient();
export const session = createSessionClient();

export type { ApiErrorResponse };
