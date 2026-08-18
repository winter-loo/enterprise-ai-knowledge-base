import { describe, expect, it, vi } from 'vitest';

import { createRagClient, createSessionClient } from './client';

describe('Project API client', () => {
	it('sends retrieval within a Project without an access capability header', async () => {
		const fetch = vi
			.fn()
			.mockResolvedValue(
				new Response(JSON.stringify({ chunks: [], retrieved: 0 }), { status: 200 })
			);
		const client = createRagClient({ fetch });

		await client.retrieve({ question: '政策', project_id: 'p-1' });

		const [path, init] = fetch.mock.calls[0] as [string, RequestInit];
		expect(path).toBe('/api/retrieve');
		expect(JSON.parse(String(init.body))).toEqual({
			question: '政策',
			project_id: 'p-1'
		});
		expect(new Headers(init.headers).get('x-scope-context')).toBeNull();
	});

	it('uses server-owned session endpoints without browser session tokens', async () => {
		const fetch = vi
			.fn()
			.mockResolvedValue(
				new Response(JSON.stringify({ session_id: 's-1', messages: [] }), { status: 200 })
			);
		const client = createSessionClient({ fetch });

		await client.getHistory('s-1');

		const [path, init] = fetch.mock.calls[0] as [string, RequestInit];
		expect(path).toBe('/api/v1/chat/history/s-1');
		expect(new Headers(init.headers).get('x-session-token')).toBeNull();
	});
});
