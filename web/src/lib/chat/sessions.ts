import type { UIMessage } from 'ai';
import type { ChatHistoryMessage, ChatPromptMessage } from '$lib/api/types';
import type { ChatScope } from './scope-policy';

const LEGACY_STORAGE_KEY = 'enterprise-kb.chat-sessions.v1';
const SESSION_STORAGE_PREFIX = 'enterprise-kb.chat-session.v1.';
const ACTIVE_KEY = 'enterprise-kb.active-session.v1';

export type LocalChatSession = {
	id: string;
	token: string;
	title: string;
	createdAt: string;
	updatedAt: string;
	scope: ChatScope;
};

function isSession(value: unknown): value is LocalChatSession {
	if (!value || typeof value !== 'object') return false;
	const session = value as Partial<LocalChatSession>;
	return Boolean(
		typeof session.id === 'string' &&
		session.id.length > 0 &&
		session.id.length <= 200 &&
		typeof session.token === 'string' &&
		/^[A-Za-z0-9._~-]{32,200}$/.test(session.token) &&
		typeof session.title === 'string' &&
		typeof session.createdAt === 'string' &&
		Number.isFinite(Date.parse(session.createdAt)) &&
		typeof session.updatedAt === 'string' &&
		Number.isFinite(Date.parse(session.updatedAt)) &&
		session.scope &&
		typeof session.scope.kbId === 'string' &&
		session.scope.kbId.length > 0 &&
		typeof session.scope.projectId === 'string' &&
		session.scope.projectId.length > 0 &&
		typeof session.scope.accessScope === 'string' &&
		session.scope.accessScope.length > 0
	);
}

export function readSessions(): LocalChatSession[] {
	if (typeof localStorage === 'undefined') return [];
	try {
		const sessions = new Map<string, LocalChatSession>();
		const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index));
		for (const key of keys) {
			if (!key?.startsWith(SESSION_STORAGE_PREFIX)) continue;
			try {
				const value: unknown = JSON.parse(localStorage.getItem(key) ?? 'null');
				if (isSession(value)) sessions.set(value.id, value);
				else localStorage.removeItem(key);
			} catch {
				localStorage.removeItem(key);
			}
		}

		let legacy: unknown = [];
		try {
			legacy = JSON.parse(localStorage.getItem(LEGACY_STORAGE_KEY) ?? '[]');
		} catch {
			localStorage.removeItem(LEGACY_STORAGE_KEY);
		}
		const legacySessions = Array.isArray(legacy) ? legacy.filter(isSession) : [];
		let migrated = true;
		for (const session of legacySessions) {
			if (!sessions.has(session.id)) {
				migrated = writeSession(session) && migrated;
				sessions.set(session.id, session);
			}
		}
		if (legacySessions.length && migrated) localStorage.removeItem(LEGACY_STORAGE_KEY);
		return [...sessions.values()].sort((left, right) =>
			right.updatedAt.localeCompare(left.updatedAt)
		);
	} catch {
		return [];
	}
}

export function writeSession(session: LocalChatSession): boolean {
	if (typeof localStorage === 'undefined') return false;
	try {
		if (!isSession(session)) return false;
		localStorage.setItem(
			`${SESSION_STORAGE_PREFIX}${encodeURIComponent(session.id)}`,
			JSON.stringify(session)
		);
		return true;
	} catch {
		return false;
	}
}

export function removeStoredSession(sessionId: string): boolean {
	if (typeof localStorage === 'undefined') return false;
	try {
		localStorage.removeItem(`${SESSION_STORAGE_PREFIX}${encodeURIComponent(sessionId)}`);
		return true;
	} catch {
		return false;
	}
}

export function isChatSessionStorageKey(key: string | null): boolean {
	return key === null || key === LEGACY_STORAGE_KEY || key.startsWith(SESSION_STORAGE_PREFIX);
}

export function readActiveSessionId(): string | null {
	if (typeof localStorage === 'undefined') return null;
	try {
		return localStorage.getItem(ACTIVE_KEY);
	} catch {
		return null;
	}
}

export function writeActiveSessionId(sessionId: string): void {
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(ACTIVE_KEY, sessionId);
	} catch {
		// Persisting the active tab is an enhancement, not a chat requirement.
	}
}

export function createLocalSession(scope: ChatScope): LocalChatSession {
	const timestamp = new Date().toISOString();
	const randomId = () => globalThis.crypto?.randomUUID?.() ?? cryptoHex(16);
	return {
		id: randomId(),
		token: cryptoHex(32),
		title: '新的研究',
		createdAt: timestamp,
		updatedAt: timestamp,
		scope: { ...scope }
	};
}

function cryptoHex(byteLength: number): string {
	if (!globalThis.crypto?.getRandomValues) {
		throw new Error('当前浏览器无法安全创建会话凭证，请使用支持 Web Crypto 的安全连接');
	}
	const bytes = globalThis.crypto.getRandomValues(new Uint8Array(byteLength));
	return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

export function titleFromQuestion(question: string): string {
	const normalized = question.replace(/\s+/g, ' ').trim();
	return normalized.length > 28 ? `${normalized.slice(0, 28)}…` : normalized || '新的研究';
}

export function historyToUIMessages(messages: ChatHistoryMessage[]): UIMessage[] {
	return messages
		.filter((message) => message.role === 'user' || message.role === 'assistant')
		.map((message, index) => ({
			id: `history-${index}-${message.created_at}`,
			role: message.role as 'user' | 'assistant',
			parts: [{ type: 'text', text: message.content, state: 'done' }]
		}));
}

export function messageText(message: UIMessage): string {
	return message.parts
		.filter(
			(part): part is Extract<(typeof message.parts)[number], { type: 'text' }> =>
				part.type === 'text'
		)
		.map((part) => part.text)
		.join('');
}

/** Converts the active AI SDK chat only; the page replaces that chat whenever scope changes. */
export function uiMessagesToPromptHistory(messages: UIMessage[]): ChatPromptMessage[] {
	return messages.flatMap((message) => {
		if (message.role !== 'user' && message.role !== 'assistant') return [];
		const content = messageText(message).trim();
		return content ? [{ role: message.role, content }] : [];
	});
}
