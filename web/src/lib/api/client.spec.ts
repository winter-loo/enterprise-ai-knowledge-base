import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, createAuthzClient, createRagClient, createSessionClient } from './client';

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
		const client = createRagClient({ fetch });

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
		const client = createRagClient({ fetch });

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
		const client = createRagClient({ fetch });

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
		const client = createRagClient({ fetch });
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
				access_scope: 'engineering',
				top_k: 4,
				history
			},
			{ signal: controller.signal }
		);

		expect(fetch).toHaveBeenCalledOnce();
		const [, init] = fetch.mock.calls[0];
		expect(init?.signal).toBe(controller.signal);
		const body = JSON.parse(String(init?.body));
		expect(body).toMatchObject({ history });
		expect(body).not.toHaveProperty('access_scope');
		expect(new Headers(init?.headers).get('x-scope-context')).toBe('engineering');
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
		const client = createSessionClient({ fetch });
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

describe('authorization scopes', () => {
	it('resolves an opaque visible scope for the authenticated principal', async () => {
		const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
			new Response(
				JSON.stringify({
					allowed: true,
					project_id: 'p-1',
					scope_context: 'engineering,general'
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			)
		);
		const client = createAuthzClient({ fetch });

		await expect(client.visibleScope({ kb_id: 'company', project_id: 'p-1' })).resolves.toEqual({
			allowed: true,
			project_id: 'p-1',
			scope_context: 'engineering,general'
		});
		expect(fetch).toHaveBeenCalledWith(
			'/api/v1/authz/visible-scope',
			expect.objectContaining({
				method: 'POST',
				body: JSON.stringify({ kb_id: 'company', project_id: 'p-1' })
			})
		);
	});
});

describe('document upload progress', () => {
	it('streams indexing progress and returns the completed result', async () => {
		const body = [
			JSON.stringify({
				stage: 'parsing',
				message: '解析文档',
				completed: 0,
				total: 1,
				percent: 10
			}),
			JSON.stringify({
				stage: 'embedding',
				message: '生成向量',
				completed: 1,
				total: 2,
				percent: 50
			}),
			JSON.stringify({
				stage: 'complete',
				message: '索引完成',
				completed: 2,
				total: 2,
				percent: 100,
				result: {
					id: 'doc-1',
					filename: 'guide.md',
					project_id: 'p-1',
					status: 'READY',
					chunk_count: 2
				}
			})
		].join('\n');
		const fetch = vi
			.fn<typeof globalThis.fetch>()
			.mockResolvedValue(new Response(body, { status: 200 }));
		const client = createRagClient({ fetch });
		const progress: number[] = [];

		const result = await client.uploadDocument(
			new File(['content'], 'guide.md'),
			{ kb_id: 'company', project_id: 'p-1', access_scope: 'general' },
			{ onProgress: (event) => progress.push(event.percent) }
		);

		expect(progress).toEqual([10, 50, 100]);
		expect(result).toMatchObject({ id: 'doc-1', chunk_count: 2 });
		expect(fetch).toHaveBeenCalledWith(
			'/api/documents/upload',
			expect.objectContaining({ method: 'POST' })
		);
	});

	it('reports browser upload bytes before consuming indexing events', async () => {
		const first = `${JSON.stringify({ stage: 'parsing', message: '解析文档', completed: 0, total: 1, percent: 10 })}\n`;
		const complete = JSON.stringify({
			stage: 'complete',
			message: '索引完成',
			completed: 1,
			total: 1,
			percent: 100,
			result: {
				id: 'doc-1',
				filename: 'guide.md',
				project_id: 'p-1',
				status: 'READY',
				chunk_count: 1
			}
		});
		const fake = {
			upload: {} as XMLHttpRequestUpload,
			responseText: '',
			status: 200,
			statusText: 'OK',
			onprogress: null as XMLHttpRequest['onprogress'],
			onload: null as XMLHttpRequest['onload'],
			onerror: null as XMLHttpRequest['onerror'],
			onabort: null as XMLHttpRequest['onabort'],
			open: vi.fn(),
			abort: vi.fn(),
			send: vi.fn()
		};
		fake.send.mockImplementation(() => {
			const uploadProgress = fake.upload.onprogress as ((event: ProgressEvent) => void) | null;
			const downloadProgress = fake.onprogress as ((event: ProgressEvent) => void) | null;
			const load = fake.onload as ((event: ProgressEvent) => void) | null;
			uploadProgress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent);
			fake.responseText = first;
			downloadProgress?.({} as ProgressEvent);
			fake.responseText += complete;
			load?.({} as ProgressEvent);
		});
		const client = createRagClient({ xhr: () => fake as unknown as XMLHttpRequest });
		const stages: string[] = [];

		await client.uploadDocument(
			new File(['content'], 'guide.md'),
			{ kb_id: 'company', project_id: 'p-1', access_scope: 'general' },
			{ onProgress: (event) => stages.push(`${event.stage}:${event.percent}`) }
		);

		expect(stages).toEqual(['uploading:5', 'parsing:10', 'complete:100']);
	});

	it('preserves FastAPI details when upload is rejected before streaming', async () => {
		const fake = {
			upload: {} as XMLHttpRequestUpload,
			responseText: JSON.stringify({ detail: '文件不能超过 10MB' }),
			status: 413,
			statusText: 'Content Too Large',
			onprogress: null as XMLHttpRequest['onprogress'],
			onload: null as XMLHttpRequest['onload'],
			onerror: null as XMLHttpRequest['onerror'],
			onabort: null as XMLHttpRequest['onabort'],
			open: vi.fn(),
			abort: vi.fn(),
			send: vi.fn()
		};
		fake.send.mockImplementation(() => {
			const load = fake.onload as ((event: ProgressEvent) => void) | null;
			load?.({} as ProgressEvent);
		});
		const client = createRagClient({ xhr: () => fake as unknown as XMLHttpRequest });
		const onProgress = vi.fn();

		await expect(
			client.uploadDocument(
				new File(['content'], 'oversized.md'),
				{ kb_id: 'company', project_id: 'p-1', access_scope: 'general' },
				{ onProgress }
			)
		).rejects.toMatchObject({ status: 413, message: '文件不能超过 10MB' });
		expect(onProgress).not.toHaveBeenCalled();
	});

	it('rejects immediately when the upload signal is already aborted', async () => {
		const fake = {
			upload: {} as XMLHttpRequestUpload,
			responseText: '',
			status: 0,
			statusText: '',
			onprogress: null as XMLHttpRequest['onprogress'],
			onload: null as XMLHttpRequest['onload'],
			onerror: null as XMLHttpRequest['onerror'],
			onabort: null as XMLHttpRequest['onabort'],
			open: vi.fn(),
			abort: vi.fn(),
			send: vi.fn()
		};
		const controller = new AbortController();
		controller.abort();
		const client = createRagClient({ xhr: () => fake as unknown as XMLHttpRequest });

		await expect(
			client.uploadDocument(
				new File(['content'], 'guide.md'),
				{ kb_id: 'company', project_id: 'p-1', access_scope: 'general' },
				{ signal: controller.signal }
			)
		).rejects.toMatchObject({ name: 'AbortError' });
		expect(fake.send).not.toHaveBeenCalled();
	});
});
