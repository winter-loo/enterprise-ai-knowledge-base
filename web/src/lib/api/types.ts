export type ChunkingStrategy = 'fixed' | 'recursive' | 'semantic' | 'paragraph';

export interface ScopePayload {
	kb_id: string;
	project_id: string;
}

/** RAG 服务的不透明访问范围：access_scope 由 authz 计算, 由调用方透传。 */
export interface RagScope extends ScopePayload {
	access_scope: string;
}

export interface HealthResponse {
	status: string;
	service: string;
}

export type VisibleScopeRequest = ScopePayload;

export interface VisibleScopeResponse {
	allowed: boolean;
	project_id: string | null;
	scope_context: string;
}

export interface KnowledgeBase {
	id: string;
	name: string;
	description: string;
	created_at: string;
}

export interface KnowledgeBaseCreateRequest {
	name: string;
	description?: string;
}

export interface KnowledgeBaseCreateResponse {
	id: string;
	name: string;
	description: string;
	default_project_id: string;
}

export interface Project {
	id: string;
	kb_id: string;
	name: string;
	description: string;
	created_at: string;
}

export interface ProjectCreateRequest {
	kb_id: string;
	name: string;
	description?: string;
}

export interface ProjectCreateResponse {
	id: string;
	name: string;
	kb_id?: string;
	description?: string;
}

export interface DocumentRecord {
	id: string;
	filename: string;
	project_id: string;
	access_scope: string;
	status: string;
	chunk_count: number;
	source_type: string;
	parser: string | null;
	pdf_type: string | null;
	chunking_strategy: ChunkingStrategy | null;
	created_at: string;
}

export interface ImportResult {
	id: string;
	filename: string;
	project_id: string;
	status: string;
	chunk_count: number;
	chunking_strategy: ChunkingStrategy;
	parser: string;
	pdf_type: string | null;
	pages_needing_ocr: number[];
}

export interface UploadDocumentRequest extends RagScope {
	chunking_strategy?: ChunkingStrategy;
}

export interface DocumentImportRequest extends RagScope {
	title: string;
	content: string;
	chunking_strategy?: ChunkingStrategy;
}

export type ChatRole = 'system' | 'user' | 'assistant';

export interface ChatPromptMessage {
	role: ChatRole;
	content: string;
}

export interface AskRequest extends RagScope {
	question: string;
	top_k?: number;
	history?: ChatPromptMessage[];
}

export interface Citation {
	id: string;
	filename: string;
	chunk_index: number;
	score: number;
	excerpt: string;
	citation_index: number;
}

export interface AskResponse {
	answer: string;
	answer_mode: string;
	citations: Citation[];
	retrieved: number;
}

export interface RetrieveChunk {
	id: string;
	filename: string;
	chunk_index: number;
	score: number;
	content: string;
	summary: string;
}

export interface RetrieveRequest extends RagScope {
	question: string;
	top_k?: number;
}

export interface RetrieveResponse {
	chunks: RetrieveChunk[];
	retrieved: number;
}

export interface EvidenceDetail {
	id: string;
	filename: string;
	chunk_index: number;
	content: string;
	summary: string;
	access_scope: string;
	project_id: string;
	document_id: string;
	source_type: string;
	source_uri: string;
	page: number | null;
	metadata: Record<string, unknown>;
	created_at: string;
}

export interface ChatCompletionRequest extends ScopePayload {
	session_id: string;
	question: string;
	department: string;
	top_k?: number;
}

export interface ChatHistoryMessage {
	role: ChatRole;
	content: string;
	created_at: string;
}

export interface ChatHistoryResponse {
	session_id: string;
	messages: ChatHistoryMessage[];
}

export interface ClearSessionResponse {
	session_id: string;
	deleted: number;
}

export interface ChatSource {
	id: string;
	filename: string;
	chunk_index: number;
	score: number;
	excerpt: string;
	citation_index: number;
}

export interface ChatSourcesEvent {
	type: 'sources';
	sources: ChatSource[];
}

export interface ChatDeltaEvent {
	type: 'delta';
	content: string;
}

export interface ChatDoneEvent {
	type: 'done';
}

export interface ChatErrorEvent {
	type: 'error';
	message: string;
}

export type ChatStreamEvent = ChatSourcesEvent | ChatDeltaEvent | ChatDoneEvent | ChatErrorEvent;

export interface ValidationIssue {
	type: string;
	loc: Array<string | number>;
	msg: string;
	input?: unknown;
	ctx?: Record<string, unknown>;
}

export interface FastApiDetailError {
	detail: string;
}

export interface FastApiValidationError {
	detail: ValidationIssue[];
}

export type ApiErrorResponse = FastApiDetailError | FastApiValidationError;
