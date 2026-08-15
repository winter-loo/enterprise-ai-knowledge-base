import type { ChatTransport, UIMessage, UIMessageChunk } from 'ai';

import { ApiError, apiErrorFromResponse } from '../api/client';
import type { ChatSource, ChatStreamEvent, ScopePayload } from '../api/types';

export interface PythonChatBody extends ScopePayload {
	session_id: string;
	session_token: string;
	top_k?: number;
}

export interface PythonChatData extends Record<string, unknown> {
	sources: ChatSource[];
}

export type PythonChatMessage = UIMessage<unknown, PythonChatData>;

const ANSWER_PART_ID = 'answer';

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isChatSource(value: unknown): value is ChatSource {
	return (
		isRecord(value) &&
		typeof value.id === 'string' &&
		typeof value.filename === 'string' &&
		typeof value.chunk_index === 'number' &&
		typeof value.score === 'number' &&
		typeof value.excerpt === 'string' &&
		typeof value.citation_index === 'number'
	);
}

function parseChatStreamEvent(data: string): ChatStreamEvent {
	let value: unknown;
	try {
		value = JSON.parse(data) as unknown;
	} catch (cause) {
		throw new Error('Chat stream contained invalid JSON.', { cause });
	}

	if (!isRecord(value) || typeof value.type !== 'string') {
		throw new Error('Chat stream contained an invalid event.');
	}

	switch (value.type) {
		case 'sources':
			if (!Array.isArray(value.sources) || !value.sources.every(isChatSource)) {
				throw new Error('Chat stream contained invalid sources.');
			}
			return { type: 'sources', sources: value.sources };
		case 'delta':
			if (typeof value.content !== 'string')
				throw new Error('Chat stream contained an invalid delta.');
			return { type: 'delta', content: value.content };
		case 'done':
			return { type: 'done' };
		case 'error':
			if (typeof value.message !== 'string')
				throw new Error('Chat stream contained an invalid error.');
			return { type: 'error', message: value.message };
		default:
			throw new Error(`Chat stream contained an unsupported event type: ${value.type}`);
	}
}

/**
 * Incrementally decodes SSE data fields without assuming network chunks align
 * with UTF-8 characters, lines, or event boundaries.
 */
export function decodeSseData(stream: ReadableStream<Uint8Array>): ReadableStream<string> {
	const decoder = new TextDecoder();
	let buffer = '';
	let dataLines: string[] = [];

	return stream.pipeThrough(
		new TransformStream<Uint8Array, string>({
			transform(chunk, controller) {
				buffer += decoder.decode(chunk, { stream: true });
				consumeLines(controller, false);
			},
			flush(controller) {
				buffer += decoder.decode();
				consumeLines(controller, true);
				if (buffer) {
					consumeLine(buffer);
					buffer = '';
				}
				dispatch(controller);
			}
		})
	);

	function dispatch(controller: TransformStreamDefaultController<string>): void {
		if (dataLines.length > 0) controller.enqueue(dataLines.join('\n'));
		dataLines = [];
	}

	function consumeLine(line: string): void {
		if (!line || line.startsWith(':')) return;

		const colon = line.indexOf(':');
		const field = colon === -1 ? line : line.slice(0, colon);
		if (field !== 'data') return;

		let value = colon === -1 ? '' : line.slice(colon + 1);
		if (value.startsWith(' ')) value = value.slice(1);
		dataLines.push(value);
	}

	function consumeLines(
		controller: TransformStreamDefaultController<string>,
		flush: boolean
	): void {
		while (true) {
			const lineEnding = findLineEnding(buffer, flush);
			if (!lineEnding) return;

			const line = buffer.slice(0, lineEnding.index);
			buffer = buffer.slice(lineEnding.index + lineEnding.length);
			if (line === '') dispatch(controller);
			else consumeLine(line);
		}
	}
}

function findLineEnding(
	value: string,
	flush: boolean
): { index: number; length: number } | undefined {
	for (let index = 0; index < value.length; index += 1) {
		const character = value[index];
		if (character === '\n') return { index, length: 1 };
		if (character !== '\r') continue;
		if (index + 1 < value.length) return { index, length: value[index + 1] === '\n' ? 2 : 1 };
		if (flush) return { index, length: 1 };
	}
	return undefined;
}

/** Maps the Python service's custom SSE protocol to Vercel AI SDK chunks. */
export function pythonSseToUiMessageStream(
	stream: ReadableStream<Uint8Array>
): ReadableStream<UIMessageChunk> {
	const events = decodeSseData(stream).getReader();
	let textStarted = false;
	let terminalEventSeen = false;

	return new ReadableStream<UIMessageChunk>({
		async start(controller) {
			controller.enqueue({ type: 'start' });

			try {
				while (true) {
					const result = await events.read();
					if (result.done) break;

					if (result.value.trim() === '[DONE]') {
						finish(controller);
						break;
					}

					let event: ChatStreamEvent;
					try {
						event = parseChatStreamEvent(result.value);
					} catch (error) {
						fail(controller, error instanceof Error ? error.message : 'Invalid chat stream event.');
						break;
					}

					switch (event.type) {
						case 'sources':
							controller.enqueue({ type: 'data-sources', data: event.sources });
							break;
						case 'delta':
							if (!event.content) break;
							startText(controller);
							controller.enqueue({ type: 'text-delta', id: ANSWER_PART_ID, delta: event.content });
							break;
						case 'done':
							finish(controller);
							break;
						case 'error':
							fail(controller, event.message);
							break;
					}

					if (terminalEventSeen) break;
				}

				if (!terminalEventSeen) fail(controller, 'Chat stream ended before a done event.');
				await events.cancel();
				controller.close();
			} catch (error) {
				controller.error(error);
			}
		},
		cancel(reason) {
			return events.cancel(reason);
		}
	});

	function startText(controller: ReadableStreamDefaultController<UIMessageChunk>): void {
		if (textStarted) return;
		textStarted = true;
		controller.enqueue({ type: 'text-start', id: ANSWER_PART_ID });
	}

	function endText(controller: ReadableStreamDefaultController<UIMessageChunk>): void {
		if (!textStarted) return;
		controller.enqueue({ type: 'text-end', id: ANSWER_PART_ID });
		textStarted = false;
	}

	function finish(controller: ReadableStreamDefaultController<UIMessageChunk>): void {
		endText(controller);
		controller.enqueue({ type: 'finish', finishReason: 'stop' });
		terminalEventSeen = true;
	}

	function fail(
		controller: ReadableStreamDefaultController<UIMessageChunk>,
		message: string
	): void {
		endText(controller);
		controller.enqueue({ type: 'error', errorText: message });
		terminalEventSeen = true;
	}
}

function lastUserQuestion(messages: UIMessage[]): string {
	for (let index = messages.length - 1; index >= 0; index -= 1) {
		const message = messages[index];
		if (message.role !== 'user') continue;

		const question = message.parts
			.filter((part) => part.type === 'text')
			.map((part) => part.text)
			.join('\n')
			.trim();
		if (question) return question;
	}

	throw new Error('A non-empty user text message is required.');
}

function requireString(body: Record<string, unknown>, field: keyof PythonChatBody): string {
	const value = body[field];
	if (typeof value !== 'string' || !value.trim())
		throw new Error(`Chat request body requires ${field}.`);
	return value;
}

function pythonRequestBody(
	body: object | undefined,
	question: string,
	chatId: string
): PythonChatBody & { question: string } {
	if (!isRecord(body)) throw new Error('Chat request body requires session and scope fields.');

	const topK = body.top_k;
	if (
		topK !== undefined &&
		(!Number.isInteger(topK) || (topK as number) < 1 || (topK as number) > 10)
	) {
		throw new Error('Chat request body top_k must be an integer from 1 to 10.');
	}

	return {
		session_id:
			typeof body.session_id === 'string' && body.session_id.trim() ? body.session_id : chatId,
		session_token: requireString(body, 'session_token'),
		kb_id: requireString(body, 'kb_id'),
		project_id: requireString(body, 'project_id'),
		department: requireString(body, 'department'),
		...(topK === undefined ? {} : { top_k: topK as number }),
		question
	};
}

export class PythonSseChatTransport implements ChatTransport<PythonChatMessage> {
	constructor(readonly endpoint = '/api/v1/chat/completions') {}

	async sendMessages({
		chatId,
		messages,
		abortSignal,
		headers,
		body
	}: Parameters<ChatTransport<PythonChatMessage>['sendMessages']>[0]): Promise<
		ReadableStream<UIMessageChunk>
	> {
		const requestHeaders = new Headers(headers);
		requestHeaders.set('content-type', 'application/json');
		requestHeaders.set('accept', 'text/event-stream');

		const response = await globalThis.fetch(this.endpoint, {
			method: 'POST',
			headers: requestHeaders,
			body: JSON.stringify(pythonRequestBody(body, lastUserQuestion(messages), chatId)),
			signal: abortSignal
		});

		if (!response.ok) throw await apiErrorFromResponse(response);
		if (!response.body) {
			throw new ApiError('Chat response body is empty.', {
				status: response.status,
				statusText: response.statusText
			});
		}

		return pythonSseToUiMessageStream(response.body);
	}

	async reconnectToStream(): Promise<ReadableStream<UIMessageChunk> | null> {
		return null;
	}
}
