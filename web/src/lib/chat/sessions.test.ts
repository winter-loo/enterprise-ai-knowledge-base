import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	createLocalSession,
	historyToUIMessages,
	messageText,
	readSessions,
	removeStoredSession,
	titleFromQuestion,
	uiMessagesToPromptHistory,
	writeSession
} from './sessions';

class MemoryStorage {
	#values = new Map<string, string>();
	get length() {
		return this.#values.size;
	}
	key(index: number) {
		return [...this.#values.keys()][index] ?? null;
	}
	getItem(key: string) {
		return this.#values.get(key) ?? null;
	}
	setItem(key: string, value: string) {
		this.#values.set(key, value);
	}
	removeItem(key: string) {
		this.#values.delete(key);
	}
	clear() {
		this.#values.clear();
	}
}

describe('chat session helpers', () => {
	beforeEach(() => vi.stubGlobal('localStorage', new MemoryStorage()));
	afterEach(() => vi.unstubAllGlobals());

	it('creates a session with an unguessable capability token', () => {
		const session = createLocalSession({ kbId: 'company', projectId: 'p-1', department: 'hr' });
		expect(session.id).toBeTruthy();
		expect(session.token.length).toBeGreaterThanOrEqual(32);
		expect(session.token).toMatch(/^[0-9a-f]+$/);
	});
	it('creates compact, readable titles', () => {
		expect(titleFromQuestion('  如何   申请年假？ ')).toBe('如何 申请年假？');
		expect(
			titleFromQuestion('这是一条用于验证标题会被安全截断的非常非常长的问题，后面不应完整显示')
		).toHaveLength(29);
	});

	it('stores sessions independently and rejects malformed capability tokens', () => {
		const first = createLocalSession({ kbId: 'company', projectId: 'p-1', department: 'hr' });
		const second = createLocalSession({ kbId: 'company', projectId: 'p-2', department: 'general' });
		expect(writeSession(first)).toBe(true);
		expect(writeSession(second)).toBe(true);
		localStorage.setItem(
			'enterprise-kb.chat-session.v1.corrupt',
			JSON.stringify({ ...first, id: 'corrupt', token: 'short' })
		);
		localStorage.setItem('enterprise-kb.chat-session.v1.invalid-json', '{');
		expect(
			readSessions()
				.map((session) => session.id)
				.sort()
		).toEqual([first.id, second.id].sort());
		expect(removeStoredSession(first.id)).toBe(true);
		expect(readSessions().map((session) => session.id)).toEqual([second.id]);
	});

	it('maps persisted history to AI SDK UI messages', () => {
		const [message] = historyToUIMessages([
			{ role: 'assistant', content: '来自制度。[1]', created_at: '2026-08-14T09:00:00Z' }
		]);
		expect(message.role).toBe('assistant');
		expect(messageText(message)).toBe('来自制度。[1]');
	});

	it('maps only non-empty user and assistant text into synchronous ask history', () => {
		expect(
			uiMessagesToPromptHistory([
				{ id: 'u1', role: 'user', parts: [{ type: 'text', text: ' 上一轮问题 ' }] },
				{
					id: 'a1',
					role: 'assistant',
					parts: [
						{ type: 'data-sources', data: [] },
						{ type: 'text', text: '上一轮回答' }
					]
				},
				{ id: 'a2', role: 'assistant', parts: [{ type: 'text', text: '   ' }] },
				{ id: 's1', role: 'system', parts: [{ type: 'text', text: '不要作为历史发送' }] }
			])
		).toEqual([
			{ role: 'user', content: '上一轮问题' },
			{ role: 'assistant', content: '上一轮回答' }
		]);
	});
});
