<script lang="ts">
	import { onDestroy } from 'svelte';
	import { toast } from 'svelte-sonner';
	import FileTextIcon from '@lucide/svelte/icons/file-text';
	import LoaderCircleIcon from '@lucide/svelte/icons/loader-circle';
	import SearchIcon from '@lucide/svelte/icons/search';
	import type { AskResponse, ChatPromptMessage } from '$lib/api/types';
	import { api } from '$lib/api/client';
	import type { ChatScope } from '$lib/chat/scope-policy';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Dialog from '$lib/components/ui/dialog';
	import { Label } from '$lib/components/ui/label';
	import { Textarea } from '$lib/components/ui/textarea';
	import MarkdownText from './MarkdownText.svelte';

	let {
		open = $bindable(false),
		scope,
		history
	}: { open: boolean; scope: ChatScope; history: ChatPromptMessage[] } = $props();
	let question = $state('');
	let topK = $state(5);
	let loading = $state(false);
	let result = $state<AskResponse>();
	let requestController: AbortController | undefined;
	let previousScopeKey = '';

	function cancelSearch(): void {
		requestController?.abort();
		requestController = undefined;
		loading = false;
		result = undefined;
	}

	$effect(() => {
		const scopeKey = `${scope.kbId}\u0000${scope.projectId}\u0000${scope.department}`;
		if (!open || (previousScopeKey && previousScopeKey !== scopeKey)) cancelSearch();
		previousScopeKey = scopeKey;
	});

	onDestroy(() => requestController?.abort());

	async function search(event: SubmitEvent) {
		event.preventDefault();
		if (!question.trim()) return;
		requestController?.abort();
		const controller = new AbortController();
		requestController = controller;
		loading = true;
		result = undefined;
		try {
			const nextResult = await api.ask(
				{
					question: question.trim(),
					kb_id: scope.kbId,
					project_id: scope.projectId,
					department: scope.department,
					top_k: topK,
					history
				},
				{ signal: controller.signal }
			);
			if (requestController === controller && !controller.signal.aborted) result = nextResult;
		} catch (error) {
			if (controller.signal.aborted) return;
			toast.error(error instanceof Error ? error.message : '检索失败');
		} finally {
			if (requestController === controller) {
				requestController = undefined;
				loading = false;
			}
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content
		class="max-h-[88vh] overflow-y-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-7 sm:max-w-3xl"
	>
		<Dialog.Header>
			<Dialog.Title class="flex items-center gap-2 font-heading text-2xl font-medium"
				><SearchIcon class="size-5 text-[var(--signal)]" />快速检索</Dialog.Title
			>
			<Dialog.Description
				>调用同步问答接口，返回完整证据摘要；没有 LLM 配置时会使用本地证据 fallback。</Dialog.Description
			>
		</Dialog.Header>
		<form class="grid gap-4" onsubmit={search}>
			<Label for="quick-question">问题</Label>
			<Textarea
				id="quick-question"
				bind:value={question}
				maxlength={2000}
				class="min-h-24 rounded-xl border border-[var(--line)] bg-white/55 p-4"
				placeholder="例如：新员工可以申请哪些设备？"
				required
			/>
			<div class="flex flex-wrap items-center justify-between gap-4">
				<label class="flex items-center gap-3 text-xs text-[var(--ink-muted)]"
					>召回片段 <input
						type="range"
						min="1"
						max="10"
						bind:value={topK}
						class="accent-[var(--signal)]"
					/><span class="w-4 font-mono">{topK}</span></label
				>
				<Button type="submit" disabled={loading || !question.trim()} class="rounded-lg"
					>{#if loading}<LoaderCircleIcon class="animate-spin" />{/if}检索并回答</Button
				>
			</div>
		</form>

		{#if result}
			<div class="grid gap-5 border-t border-[var(--line)] pt-5">
				<div class="flex flex-wrap gap-2">
					<Badge>{result.answer_mode}</Badge><Badge variant="secondary"
						>命中 {result.retrieved} 个片段</Badge
					>
				</div>
				<MarkdownText content={result.answer} />
				{#if result.citations.length}
					<div class="grid gap-2 sm:grid-cols-2">
						{#each result.citations as citation, index (citation.id)}
							<div class="rounded-xl border border-[var(--line)] bg-white/55 p-4">
								<div class="flex items-center gap-2 text-xs font-semibold">
									<FileTextIcon class="size-4 text-[var(--signal)]" />[{index + 1}] {citation.filename}<span
										class="ml-auto font-mono text-[10px] text-[var(--ink-faint)]"
										>{Math.round(citation.score * 100)}%</span
									>
								</div>
								<p class="mt-2 text-xs leading-5 text-[var(--ink-muted)]">{citation.excerpt}</p>
							</div>
						{/each}
					</div>
				{/if}
			</div>
		{/if}
	</Dialog.Content>
</Dialog.Root>
