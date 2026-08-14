import type { UIMessageChunk } from 'ai';
import { describe, expect, it, vi } from 'vitest';

import { PythonSseChatTransport, pythonSseToUiMessageStream } from './python-sse-transport';

function chunkBytes(text: string, widths: number[]): ReadableStream<Uint8Array> {
	const bytes = new TextEncoder().encode(text);
	let offset = 0;
	let widthIndex = 0;

	return new ReadableStream({
		pull(controller) {
			if (offset >= bytes.length) {
				controller.close();
				return;
			}

			const width = widths[widthIndex++ % widths.length];
			controller.enqueue(bytes.slice(offset, offset + width));
			offset += width;
		}
	});
}

async function collect(stream: ReadableStream<UIMessageChunk>): Promise<UIMessageChunk[]> {
	const chunks: UIMessageChunk[] = [];
	for await (const chunk of stream) chunks.push(chunk);
	return chunks;
}

describe('Python SSE to AI SDK stream', () => {
	it('handles byte splits, CRLF, multi-line data, and a final event at EOF', async () => {
		const source = {
			id: 'chunk-1',
			filename: 'guide.md',
			chunk_index: 0,
			score: 0.91,
			excerpt: '重启前保存配置。'
		};
		const wire = [
			`data: ${JSON.stringify({ type: 'sources', sources: [source] })}\r\n\r\n`,
			'data: {"type":"delta",\r\n',
			'data: "content":"你好"}\r\n\r\n',
			'data: {"type":"done"}'
		].join('');

		await expect(
			collect(pythonSseToUiMessageStream(chunkBytes(wire, [1, 2, 5, 3])))
		).resolves.toEqual([
			{ type: 'start' },
			{ type: 'data-sources', data: [source] },
			{ type: 'text-start', id: 'answer' },
			{ type: 'text-delta', id: 'answer', delta: '你好' },
			{ type: 'text-end', id: 'answer' },
			{ type: 'finish', finishReason: 'stop' }
		]);
	});

	it('maps a Python error event to an AI SDK error and does not finish successfully', async () => {
		const wire = [
			'data: {"type":"sources","sources":[]}\n\n',
			'data: {"type":"error","message":"LLM configuration is required"}\n\n'
		].join('');

		await expect(collect(pythonSseToUiMessageStream(chunkBytes(wire, [7])))).resolves.toEqual([
			{ type: 'start' },
			{ type: 'data-sources', data: [] },
			{ type: 'error', errorText: 'LLM configuration is required' }
		]);
	});

	it('reports a truncated stream instead of presenting a partial answer as complete', async () => {
		const wire = 'data: {"type":"delta","content":"partial"}\n\n';

		await expect(collect(pythonSseToUiMessageStream(chunkBytes(wire, [4])))).resolves.toEqual([
			{ type: 'start' },
			{ type: 'text-start', id: 'answer' },
			{ type: 'text-delta', id: 'answer', delta: 'partial' },
			{ type: 'text-end', id: 'answer' },
			{ type: 'error', errorText: 'Chat stream ended before a done event.' }
		]);
	});
});

describe('Python SSE chat request', () => {
	it('forwards the scoped session capability token', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
			new Response('data: {"type":"done"}\n\n', {
				status: 200,
				headers: { 'content-type': 'text/event-stream' }
			})
		);
		const transport = new PythonSseChatTransport();
		await transport.sendMessages({
			chatId: 's-1',
			messages: [{ id: 'u-1', role: 'user', parts: [{ type: 'text', text: '问题' }] }],
			body: {
				session_id: 's-1',
				session_token: 't'.repeat(32),
				kb_id: 'company',
				project_id: 'p-1',
				department: 'engineering'
			},
			trigger: 'submit-message',
			messageId: 'u-1',
			abortSignal: undefined
		});

		expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toMatchObject({
			session_token: 't'.repeat(32)
		});
		fetch.mockRestore();
	});
});
