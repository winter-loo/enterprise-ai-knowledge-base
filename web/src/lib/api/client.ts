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
	IndexProgressEvent,
	KnowledgeBase,
	KnowledgeBaseCreateRequest,
	KnowledgeBaseCreateResponse,
	Project,
	ProjectCreateRequest,
	ProjectCreateResponse,
	RagScope,
	RetrieveRequest,
	RetrieveResponse,
	UploadDocumentRequest,
	ValidationIssue,
	VisibleScopeRequest,
	VisibleScopeResponse
} from './types';

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
	baseUrl?: string;
	fetch?: FetchLike;
	xhr?: () => XMLHttpRequest;
}

export interface ApiRequestOptions {
	signal?: AbortSignal;
}

export interface UploadRequestOptions extends ApiRequestOptions {
	onProgress?: (event: IndexProgressEvent) => void;
}

/** 会话服务仍使用 department 作为会话范围字段(与 RAG 的 access_scope 分离)。 */
export interface SessionAccess {
	kb_id: string;
	project_id: string;
	department: string;
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
	response(path: string, init?: RequestInit): Promise<Response>;
	request<T>(path: string, init?: RequestInit): Promise<T>;
	jsonRequest<T>(
		path: string,
		payload: object,
		requestOptions?: ApiRequestOptions,
		headers?: Record<string, string>
	): Promise<T>;
}

function createTransport(options: ApiClientOptions): RequestTransport {
	const baseUrl = options.baseUrl ?? '';
	const fetchResponse = options.fetch ?? ((input, init) => globalThis.fetch(input, init));

	async function response(path: string, init: RequestInit = {}): Promise<Response> {
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
		return response;
	}

	async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
		const result = await response(path, init);

		const text = await result.text();
		if (!text) return undefined as T;

		const body = parseResponseBody(text);
		if (typeof body === 'string') {
			throw new ApiError('Expected a JSON response from the API', {
				status: result.status,
				statusText: result.statusText,
				body
			});
		}
		return body as T;
	}

	function jsonRequest<T>(
		path: string,
		payload: object,
		requestOptions?: ApiRequestOptions,
		headers?: Record<string, string>
	): Promise<T> {
		return request<T>(path, {
			method: 'POST',
			headers: { 'content-type': 'application/json', ...headers },
			body: JSON.stringify(payload),
			signal: requestOptions?.signal
		});
	}

	return { response, request, jsonRequest };
}

function scopeHeader(accessScope: string): Record<string, string> {
	return { 'x-scope-context': accessScope };
}

function createProgressConsumer(options?: UploadRequestOptions): {
	consume(line: string): void;
	result(): ImportResult | undefined;
} {
	let completed: ImportResult | undefined;
	return {
		consume(line) {
			if (!line.trim()) return;
			const event = JSON.parse(line) as IndexProgressEvent;
			options?.onProgress?.(event);
			if (event.stage === 'error') {
				throw new ApiError(event.message, { status: event.status ?? 500, body: event });
			}
			if (event.stage === 'complete') completed = event.result;
		},
		result: () => completed
	};
}

function uploadWithXhr(
	url: string,
	form: FormData,
	options: UploadRequestOptions | undefined,
	createXhr: () => XMLHttpRequest
): Promise<ImportResult> {
	return new Promise((resolve, reject) => {
		const xhr = createXhr();
		const progress = createProgressConsumer(options);
		let offset = 0;
		let buffer = '';
		let settled = false;

		const fail = (error: unknown): void => {
			if (settled) return;
			settled = true;
			reject(error);
		};
		const consumeResponse = (final: boolean): void => {
			buffer += xhr.responseText.slice(offset);
			offset = xhr.responseText.length;
			const lines = buffer.split('\n');
			buffer = lines.pop() ?? '';
			for (const line of lines) progress.consume(line);
			if (final) {
				progress.consume(buffer);
				buffer = '';
			}
		};

		xhr.open('POST', url);
		xhr.upload.onprogress = (event) => {
			const percent =
				event.lengthComputable && event.total > 0
					? Math.min(10, Math.round((10 * event.loaded) / event.total))
					: 0;
			options?.onProgress?.({
				stage: 'uploading',
				message: '上传文件',
				completed: event.loaded,
				total: event.total,
				percent
			});
		};
		xhr.onprogress = () => {
			try {
				consumeResponse(false);
			} catch (error) {
				fail(error);
				xhr.abort();
			}
		};
		xhr.onload = () => {
			try {
				consumeResponse(true);
				if (xhr.status < 200 || xhr.status >= 300) {
					fail(
						new ApiError(xhr.statusText || '上传失败', {
							status: xhr.status,
							body: xhr.responseText
						})
					);
					return;
				}
				const result = progress.result();
				if (!result) throw new ApiError('上传进度流未返回索引结果', { status: 502 });
				settled = true;
				resolve(result);
			} catch (error) {
				fail(error);
			}
		};
		xhr.onerror = () => fail(new ApiError('Network request failed', { status: 0 }));
		xhr.onabort = () => fail(new DOMException('The operation was aborted', 'AbortError'));
		options?.signal?.addEventListener('abort', () => xhr.abort(), { once: true });
		if (options?.signal?.aborted) xhr.abort();
		else xhr.send(form);
	});
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
		options?: UploadRequestOptions
	): Promise<ImportResult>;
	importDocument(
		payload: DocumentImportRequest,
		options?: ApiRequestOptions
	): Promise<ImportResult>;
	ask(payload: AskRequest, options?: ApiRequestOptions): Promise<AskResponse>;
	retrieve(payload: RetrieveRequest, options?: ApiRequestOptions): Promise<RetrieveResponse>;
	getEvidence(
		chunkId: string,
		scope: RagScope,
		options?: ApiRequestOptions
	): Promise<EvidenceDetail>;
}

export interface SessionApiClient {
	health(options?: ApiRequestOptions): Promise<HealthResponse>;
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

export interface AuthzApiClient {
	visibleScope(
		payload: VisibleScopeRequest,
		options?: ApiRequestOptions
	): Promise<VisibleScopeResponse>;
}

export function createAuthzClient(options: ApiClientOptions = {}): AuthzApiClient {
	const { jsonRequest } = createTransport(options);
	return {
		visibleScope: (payload, requestOptions) =>
			jsonRequest('/api/v1/authz/visible-scope', payload, requestOptions)
	};
}

export function createRagClient(options: ApiClientOptions = {}): RagApiClient {
	const { response, request, jsonRequest } = createTransport(options);

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
		uploadDocument: async (file, payload, requestOptions) => {
			const form = new FormData();
			form.set('file', file);
			form.set('kb_id', payload.kb_id);
			form.set('project_id', payload.project_id);
			form.set('access_scope', payload.access_scope);
			form.set('chunking_strategy', payload.chunking_strategy ?? 'recursive');
			const uploadPath = '/api/documents/upload';
			if (
				options.xhr !== undefined ||
				(options.fetch === undefined && typeof XMLHttpRequest !== 'undefined')
			) {
				return uploadWithXhr(
					joinUrl(options.baseUrl ?? '', uploadPath),
					form,
					requestOptions,
					options.xhr ?? (() => new XMLHttpRequest())
				);
			}
			const uploadResponse = await response(uploadPath, {
				method: 'POST',
				body: form,
				signal: requestOptions?.signal
			});
			if (!uploadResponse.body) {
				throw new ApiError('上传响应缺少进度流', { status: uploadResponse.status });
			}
			const reader = uploadResponse.body.getReader();
			const decoder = new TextDecoder();
			let buffer = '';
			const progress = createProgressConsumer(requestOptions);

			while (true) {
				const { done, value } = await reader.read();
				buffer += decoder.decode(value, { stream: !done });
				const lines = buffer.split('\n');
				buffer = lines.pop() ?? '';
				for (const line of lines) progress.consume(line);
				if (done) break;
			}
			progress.consume(buffer);
			const result = progress.result();
			if (!result) throw new ApiError('上传进度流未返回索引结果', { status: 502 });
			return result;
		},
		importDocument: (payload, requestOptions) =>
			jsonRequest('/api/v1/document/import', payload, requestOptions),
		ask: (payload, requestOptions) => {
			const { access_scope, ...body } = payload;
			return jsonRequest('/api/ask', body, requestOptions, scopeHeader(access_scope));
		},
		retrieve: (payload, requestOptions) => {
			const { access_scope, ...body } = payload;
			return jsonRequest('/api/retrieve', body, requestOptions, scopeHeader(access_scope));
		},
		getEvidence: (chunkId, scope, requestOptions) =>
			request(
				`/api/evidence/${encodeURIComponent(chunkId)}?${new URLSearchParams({
					kb_id: scope.kb_id,
					project_id: scope.project_id
				})}`,
				{
					headers: scopeHeader(scope.access_scope),
					signal: requestOptions?.signal
				}
			)
	};
}

export function createSessionClient(options: ApiClientOptions = {}): SessionApiClient {
	const { request } = createTransport(options);

	return {
		health: (requestOptions) => request('/api/v1/chat/health', { signal: requestOptions?.signal }),
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
export const authz = createAuthzClient();

export type { ApiErrorResponse };
