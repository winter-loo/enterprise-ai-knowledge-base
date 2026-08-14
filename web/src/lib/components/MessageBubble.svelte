<script lang="ts">
	import type { UIMessage } from 'ai';
	import FileTextIcon from '@lucide/svelte/icons/file-text';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';
	import { Badge } from '$lib/components/ui/badge';
	import type { ChatSource } from '$lib/api/types';
	import { messageText } from '$lib/chat/sessions';
	import BrandMark from './BrandMark.svelte';
	import MarkdownText from './MarkdownText.svelte';

	let {
		message,
		streaming = false,
		incomplete = false
	}: { message: UIMessage; streaming?: boolean; incomplete?: boolean } = $props();
	let text = $derived(messageText(message));
	let sources = $derived(
		message.parts.flatMap((part) =>
			part.type === 'data-sources' ? ((part.data as ChatSource[]) ?? []) : []
		)
	);
</script>

{#if message.role === 'user'}
	<article class="message-enter ml-auto max-w-[min(78%,42rem)]">
		<div
			class="rounded-[1.35rem] rounded-br-sm bg-[var(--ink)] px-5 py-3.5 text-[15px] leading-6 text-[#f7f3ea] shadow-[0_8px_28px_rgba(27,46,42,.12)]"
		>
			{text}
		</div>
	</article>
{:else if message.role === 'assistant'}
	<article class="message-enter group grid grid-cols-[36px_minmax(0,1fr)] gap-4">
		<div class="mt-0.5 text-[var(--ink)]"><BrandMark /></div>
		<div class="min-w-0">
			<div class="mb-2 flex items-center gap-2">
				<span class="text-[11px] font-bold tracking-[0.16em] text-[var(--ink-muted)] uppercase"
					>知识助手</span
				>
				{#if streaming}
					<span class="flex items-center gap-1.5 text-[11px] text-[var(--signal)]">
						<span class="stream-dot"></span> 正在组织答案
					</span>
				{/if}
				{#if incomplete}
					<Badge variant="destructive" class="h-5 rounded-full px-2 text-[9px]">回答不完整</Badge>
				{/if}
			</div>
			{#if text}
				<MarkdownText content={text} />
			{:else if streaming}
				<div class="flex h-8 items-center gap-1.5" aria-label="正在生成">
					<span class="thinking-bar"></span><span class="thinking-bar"></span><span
						class="thinking-bar"
					></span>
				</div>
			{/if}
			{#if incomplete}
				<p class="mt-3 text-xs leading-5 text-destructive">
					流式生成未正常完成，请勿把上方内容视为最终答案；可重新提问获取完整结果。
				</p>
			{/if}

			{#if sources.length}
				<div class="mt-5 border-t border-[var(--line)] pt-4">
					<div
						class="mb-3 flex items-center gap-2 text-[11px] font-bold tracking-[0.14em] text-[var(--ink-muted)] uppercase"
					>
						<SparklesIcon class="size-3.5 text-[var(--signal)]" />
						参考片段 · {sources.length}
					</div>
					<div class="grid gap-2 sm:grid-cols-2">
						{#each sources as source, index (`${source.filename}-${source.chunk_index}-${index}`)}
							<div class="source-card rounded-xl border border-[var(--line)] bg-white/55 p-3.5">
								<div class="flex min-w-0 items-center gap-2">
									<span
										class="grid size-6 shrink-0 place-items-center rounded-md bg-[var(--signal-soft)] text-[var(--signal)]"
										><FileTextIcon class="size-3.5" /></span
									>
									<span class="truncate text-xs font-semibold">[{index + 1}] {source.filename}</span
									>
									<Badge
										variant="outline"
										class="ml-auto h-5 rounded-full px-1.5 font-mono text-[9px] tracking-normal normal-case"
										>{Math.round(source.score * 100)}%</Badge
									>
								</div>
								{#if source.excerpt}
									<p class="mt-2 line-clamp-3 text-xs leading-5 text-[var(--ink-muted)]">
										{source.excerpt}
									</p>
								{/if}
							</div>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	</article>
{/if}
