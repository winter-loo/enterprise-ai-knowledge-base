import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, createApiClient } from './client';

describe('API errors', () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	it('preserves a FastAPI detail string', async () => {
		const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
			new Response(JSON.stringify({ detail: '项目范围不存在' }), {
				status: 404,
				headers: { 'content-type': 'application/json' }
			})
		);
		const client = createApiClient({ fetch });

		await expect(client.listProjects('missing')).rejects.toMatchObject({
			name: 'ApiError',
			status: 404,
			message: '项目范围不存在',
			body: { detail: '项目范围不存在' }
		} satisfies Partial<ApiError>);
	});

	it('formats every Pydantic validation issue', async () => {
		const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
			new Response(
				JSON.stringify({
					detail: [
						{
							type: 'string_too_short',
							loc: ['body', 'name'],
							msg: 'String should have at least 1 character'
						},
						{
							type: 'less_than_equal',
							loc: ['body', 'top_k'],
							msg: 'Input should be less than or equal to 10'
						}
					]
				}),
				{ status: 422, headers: { 'content-type': 'application/json' } }
			)
		);
		const client = createApiClient({ fetch });

		await expect(client.createKnowledgeBase({ name: '' })).rejects.toThrow(
			'body.name: String should have at least 1 character; body.top_k: Input should be less than or equal to 10'
		);
	});

	it('retains a non-JSON server error instead of hiding it behind a parse failure', async () => {
		const fetch = vi
			.fn<typeof globalThis.fetch>()
			.mockResolvedValue(
				new Response('Database unavailable', { status: 500, statusText: 'Internal Server Error' })
			);
		const client = createApiClient({ fetch });

		await expect(client.health()).rejects.toMatchObject({
			status: 500,
			message: 'Database unavailable',
			body: 'Database unavailable'
		});
	});
});

describe('synchronous ask requests', () => {
	it('forwards scoped conversation history and the cancellation signal', async () => {
		const fetch = vi
			.fn<typeof globalThis.fetch>()
			.mockResolvedValue(
				new Response(
					JSON.stringify({ answer: 'answer', answer_mode: 'test', citations: [], retrieved: 0 }),
					{ status: 200, headers: { 'content-type': 'application/json' } }
				)
			);
		const client = createApiClient({ fetch });
		const controller = new AbortController();
		const history = [
			{ role: 'user' as const, content: '上一轮问题' },
			{ role: 'assistant' as const, content: '上一轮回答' }
		];

		await client.ask(
			{
				question: '继续说明',
				kb_id: 'company',
				project_id: 'project-1',
				department: 'engineering',
				top_k: 4,
				history
			},
			{ signal: controller.signal }
		);

		expect(fetch).toHaveBeenCalledOnce();
		const [, init] = fetch.mock.calls[0];
		expect(init?.signal).toBe(controller.signal);
		expect(JSON.parse(String(init?.body))).toMatchObject({ history });
	});
});

describe('persistent chat sessions', () => {
	it('includes the session scope when reading and clearing history', async () => {
		const fetch = vi
			.fn<typeof globalThis.fetch>()
			.mockResolvedValueOnce(
				new Response(JSON.stringify({ session_id: 's-1', messages: [] }), { status: 200 })
			)
			.mockResolvedValueOnce(
				new Response(JSON.stringify({ session_id: 's-1', deleted: 0 }), { status: 200 })
			);
		const client = createApiClient({ fetch });
		const scope = {
			kb_id: 'company',
			project_id: 'p-1',
			department: 'engineering',
			session_token: 't'.repeat(32)
		};

		await client.getHistory('s-1', scope);
		await client.clearSession('s-1', scope);

		for (const [url] of fetch.mock.calls) {
			expect(String(url)).toContain('kb_id=company');
			expect(String(url)).toContain('project_id=p-1');
			expect(String(url)).toContain('department=engineering');
			expect(String(url)).not.toContain('session_token');
		}
		expect(new Headers(fetch.mock.calls[0][1]?.headers).get('x-session-token')).toBe(
			't'.repeat(32)
		);
		expect(fetch.mock.calls[1][1]?.method).toBe('DELETE');
	});
});
