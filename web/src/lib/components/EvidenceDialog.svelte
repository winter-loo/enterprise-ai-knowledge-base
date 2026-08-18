<script lang="ts">
	import FileTextIcon from '@lucide/svelte/icons/file-text';
	import LoaderCircleIcon from '@lucide/svelte/icons/loader-circle';
	import { rag } from '$lib/api/client';
	import type { ChatSource, EvidenceDetail, ProjectReference } from '$lib/api/types';
	import { Badge } from '$lib/components/ui/badge';
	import * as Dialog from '$lib/components/ui/dialog';
	import MarkdownText from './MarkdownText.svelte';

	let {
		open = $bindable(false),
		source = null,
		project
	}: { open: boolean; source: ChatSource | null; project: ProjectReference } = $props();

	let detail = $state<EvidenceDetail>();
	let loading = $state(false);
	let error = $state('');

	$effect(() => {
		if (!open || !source) return;
		const chunkId = source.id;
		let cancelled = false;
		detail = undefined;
		error = '';
		loading = true;
		rag
			.getEvidence(chunkId, {
				project_id: project.projectId
			})
			.then((next) => {
				if (!cancelled) detail = next;
			})
			.catch((reason: unknown) => {
				if (!cancelled) error = reason instanceof Error ? reason.message : '加载参考资料失败';
			})
			.finally(() => {
				if (!cancelled) loading = false;
			});
		return () => {
			cancelled = true;
		};
	});

	let chunkingStrategy = $derived(
		typeof detail?.metadata?.chunking_strategy === 'string'
			? detail.metadata.chunking_strategy
			: undefined
	);
	let sourceDate = $derived(detail ? new Date(detail.created_at).toLocaleString('zh-CN') : '');
</script>

<Dialog.Root bind:open>
	<Dialog.Content
		class="max-h-[88vh] overflow-y-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-7 sm:max-w-3xl"
	>
		<Dialog.Header>
			<Dialog.Title class="flex items-center gap-2 font-heading text-xl font-medium"
				><FileTextIcon class="size-5 text-[var(--signal)]" />参考资料详情</Dialog.Title
			>
			<Dialog.Description>
				{#if source}「{source.filename}」第 {source.chunk_index + 1} 个片段的完整内容与来源信息{/if}
			</Dialog.Description>
		</Dialog.Header>

		{#if loading}
			<div class="grid place-items-center py-10">
				<LoaderCircleIcon class="size-6 animate-spin text-[var(--signal)]" />
			</div>
		{:else if error}
			<p class="text-sm text-destructive">{error}</p>
		{:else if detail}
			<div class="grid gap-5">
				<div class="flex flex-wrap gap-2">
					<Badge variant="secondary"
						>[{source?.citation_index}] 片段 #{detail.chunk_index + 1}</Badge
					>
					{#if source}<Badge variant="outline">相关度 {Math.round(source.score * 100)}%</Badge>{/if}
					{#if detail.source_type}<Badge variant="outline">{detail.source_type}</Badge>{/if}
					{#if chunkingStrategy}<Badge variant="outline">{chunkingStrategy}</Badge>{/if}
				</div>

				<section>
					<h3
						class="mb-2 text-[11px] font-bold tracking-[0.14em] text-[var(--ink-muted)] uppercase"
					>
						正文
					</h3>
					<div
						class="max-h-[40vh] overflow-y-auto rounded-xl border border-[var(--line)] bg-white/55 p-4"
					>
						<MarkdownText content={detail.content} />
					</div>
				</section>

				{#if detail.summary}
					<section>
						<h3
							class="mb-2 text-[11px] font-bold tracking-[0.14em] text-[var(--ink-muted)] uppercase"
						>
							摘要
						</h3>
						<p
							class="rounded-xl border border-[var(--line)] bg-white/55 p-4 text-sm leading-6 text-[var(--ink-muted)]"
						>
							{detail.summary}
						</p>
					</section>
				{/if}

				<section>
					<h3
						class="mb-2 text-[11px] font-bold tracking-[0.14em] text-[var(--ink-muted)] uppercase"
					>
						来源信息
					</h3>
					<dl class="grid gap-2 rounded-xl border border-[var(--line)] bg-white/55 p-4 text-sm">
						<div class="flex gap-3">
							<dt class="w-24 shrink-0 text-[var(--ink-faint)]">文件</dt>
							<dd class="min-w-0 break-all">{detail.filename}</dd>
						</div>
						{#if detail.page != null}
							<div class="flex gap-3">
								<dt class="w-24 shrink-0 text-[var(--ink-faint)]">页码</dt>
								<dd>第 {detail.page} 页</dd>
							</div>
						{/if}
						<div class="flex gap-3">
							<dt class="w-24 shrink-0 text-[var(--ink-faint)]">入库时间</dt>
							<dd>{sourceDate}</dd>
						</div>
					</dl>
				</section>
			</div>
		{/if}
	</Dialog.Content>
</Dialog.Root>
