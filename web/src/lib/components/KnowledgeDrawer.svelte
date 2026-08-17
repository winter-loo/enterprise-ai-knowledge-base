<script lang="ts">
	import { toast } from 'svelte-sonner';
	import BracesIcon from '@lucide/svelte/icons/braces';
	import CheckCircle2Icon from '@lucide/svelte/icons/check-circle-2';
	import CloudUploadIcon from '@lucide/svelte/icons/cloud-upload';
	import FileIcon from '@lucide/svelte/icons/file';
	import FilePlus2Icon from '@lucide/svelte/icons/file-plus-2';
	import LoaderCircleIcon from '@lucide/svelte/icons/loader-circle';
	import SearchIcon from '@lucide/svelte/icons/search';
	import type {
		ChunkingStrategy,
		DocumentRecord,
		ImportResult,
		IndexProgressEvent
	} from '$lib/api/types';
	import { rag } from '$lib/api/client';
	import type { ChatScope } from '$lib/chat/scope-policy';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import * as Select from '$lib/components/ui/select';
	import * as Sheet from '$lib/components/ui/sheet';
	import { Textarea } from '$lib/components/ui/textarea';

	let {
		open = $bindable(false),
		documents,
		scope,
		onImported
	}: {
		open: boolean;
		documents: DocumentRecord[];
		scope: ChatScope;
		onImported: (result: ImportResult) => Promise<void> | void;
	} = $props();

	let tab = $state<'files' | 'upload' | 'paste'>('files');
	let strategy = $state<ChunkingStrategy>('recursive');
	let fileList = $state<FileList>();
	let title = $state('');
	let content = $state('');
	let query = $state('');
	let busy = $state(false);
	let indexProgress = $state<IndexProgressEvent>();

	const strategies: Array<{ value: ChunkingStrategy; label: string; help: string }> = [
		{ value: 'recursive', label: '递归结构', help: '按段落、换行和标点保留自然结构' },
		{ value: 'fixed', label: '固定窗口', help: '稳定的重叠长度，适合格式简单的材料' },
		{ value: 'semantic', label: '语义向量', help: '按话题变化切分，索引时间更长' },
		{ value: 'paragraph', label: '标题段落', help: '保留 Markdown 标题上下文' }
	];

	let strategyLabel = $derived(
		strategies.find((item) => item.value === strategy)?.label ?? strategy
	);
	let filteredDocuments = $derived(
		documents.filter(
			(document) =>
				document.project_id === scope.projectId &&
				document.filename.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
		)
	);

	function createdLabel(value: string): string {
		const date = new Date(value);
		return Number.isNaN(date.getTime())
			? value
			: new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(date);
	}

	async function upload() {
		const file = fileList?.[0];
		if (!file) return toast.error('请先选择一个文档');
		busy = true;
		indexProgress = {
			stage: 'parsing',
			message: '上传文件',
			completed: 0,
			total: 1,
			percent: 0
		};
		let result: ImportResult;
		try {
			result = await rag.uploadDocument(
				file,
				{
					kb_id: scope.kbId,
					project_id: scope.projectId,
					access_scope: 'general',
					chunking_strategy: strategy
				},
				{
					onProgress: (event) => (indexProgress = event)
				}
			);
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '上传失败');
			busy = false;
			return;
		}
		fileList = undefined;
		indexProgress = undefined;
		toast.success(`已索引 ${result.chunk_count} 个知识片段`);
		if (result.pages_needing_ocr.length) {
			toast.warning(
				`第 ${result.pages_needing_ocr.join('、')} 页需要 OCR，当前索引可能缺少这些页面的内容`
			);
		}
		tab = 'files';
		try {
			await onImported(result);
		} catch (error) {
			toast.warning(
				error instanceof Error
					? `已索引，但刷新失败：${error.message}`
					: '已索引，但资料列表刷新失败'
			);
		} finally {
			busy = false;
		}
	}

	async function importText() {
		if (!title.trim() || !content.trim()) return;
		busy = true;
		let result: ImportResult;
		try {
			result = await rag.importDocument({
				title: title.trim(),
				content: content.trim(),
				kb_id: scope.kbId,
				project_id: scope.projectId,
				access_scope: 'general',
				chunking_strategy: strategy
			});
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '导入失败');
			busy = false;
			return;
		}
		title = '';
		content = '';
		toast.success(`已导入 ${result.chunk_count} 个知识片段`);
		tab = 'files';
		try {
			await onImported(result);
		} catch (error) {
			toast.warning(
				error instanceof Error
					? `已导入，但刷新失败：${error.message}`
					: '已导入，但资料列表刷新失败'
			);
		} finally {
			busy = false;
		}
	}
</script>

<Sheet.Root bind:open>
	<Sheet.Content
		class="w-full border-[var(--line)] bg-[var(--paper)] sm:max-w-xl"
		showCloseButton={true}
	>
		<Sheet.Header class="border-b border-[var(--line)] px-6 py-5 pr-16">
			<Sheet.Title class="font-heading text-2xl font-medium">知识资料</Sheet.Title>
			<Sheet.Description>浏览当前项目资料，或把新证据加入检索索引。</Sheet.Description>
		</Sheet.Header>

		<div class="flex border-b border-[var(--line)] px-6" role="tablist" aria-label="知识资料操作">
			{#each [{ id: 'files', label: '资料库' }, { id: 'upload', label: '上传文件' }, { id: 'paste', label: '粘贴文本' }] as item (item.id)}
				<button
					type="button"
					class:active-tab={tab === item.id}
					class="drawer-tab px-4 py-3 text-xs font-bold tracking-[0.12em] text-[var(--ink-muted)] uppercase"
					onclick={() => (tab = item.id as typeof tab)}
				>
					{item.label}
				</button>
			{/each}
		</div>

		<div class="min-h-0 flex-1 overflow-y-auto p-6">
			{#if tab === 'files'}
				<div class="relative mb-5">
					<SearchIcon
						class="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-[var(--ink-faint)]"
					/>
					<Input
						bind:value={query}
						placeholder="搜索当前项目资料"
						class="rounded-xl border border-[var(--line)] bg-white/55 py-2 pl-10"
					/>
				</div>
				{#if filteredDocuments.length}
					<div class="grid gap-2.5">
						{#each filteredDocuments as document (document.id)}
							<article
								class="rounded-xl border border-[var(--line)] bg-white/55 p-4 transition hover:border-[var(--line-strong)] hover:bg-white/80"
							>
								<div class="flex items-start gap-3">
									<div
										class="grid size-9 shrink-0 place-items-center rounded-lg bg-[var(--signal-soft)] text-[var(--signal)]"
									>
										<FileIcon class="size-4" />
									</div>
									<div class="min-w-0 flex-1">
										<div class="flex items-center gap-2">
											<h3 class="truncate text-sm font-semibold">{document.filename}</h3>
											<CheckCircle2Icon class="size-3.5 shrink-0 text-[var(--success)]" />
										</div>
										<p class="mt-1 text-[11px] leading-5 text-[var(--ink-muted)]">
											{document.chunk_count} 个片段 · {document.access_scope} · {document.chunking_strategy ??
												'recursive'}
										</p>
										<div class="mt-2 flex flex-wrap gap-2">
											<Badge variant="secondary">{document.parser ?? document.source_type}</Badge>
											{#if document.pdf_type}<Badge variant="secondary">{document.pdf_type}</Badge
												>{/if}
											<span class="text-[10px] text-[var(--ink-faint)]"
												>{createdLabel(document.created_at)}</span
											>
										</div>
									</div>
								</div>
							</article>
						{/each}
					</div>
				{:else}
					<div
						class="grid min-h-72 place-items-center rounded-2xl border border-dashed border-[var(--line-strong)] p-8 text-center"
					>
						<div>
							<FilePlus2Icon class="mx-auto mb-3 size-8 text-[var(--signal)]" />
							<h3 class="font-heading text-xl">还没有资料</h3>
							<p class="mt-2 text-sm text-[var(--ink-muted)]">
								上传文档或粘贴文本，开始建立项目知识。
							</p>
						</div>
					</div>
				{/if}
			{:else}
				<div class="grid gap-6">
					<div class="grid gap-2">
						<Label>切片策略</Label>
						<Select.Root type="single" bind:value={strategy}>
							<Select.Trigger class="w-full rounded-xl border border-[var(--line)] px-3"
								><span>{strategyLabel}</span></Select.Trigger
							>
							<Select.Content
								>{#each strategies as item (item.value)}<Select.Item
										value={item.value}
										label={item.label}
									/>{/each}</Select.Content
							>
						</Select.Root>
						<p class="text-xs leading-5 text-[var(--ink-muted)]">
							{strategies.find((item) => item.value === strategy)?.help}
						</p>
					</div>

					{#if tab === 'upload'}
						<div
							class="rounded-2xl border border-dashed border-[var(--line-strong)] bg-white/40 p-8 text-center"
						>
							<CloudUploadIcon class="mx-auto mb-3 size-8 text-[var(--signal)]" />
							<h3 class="font-heading text-xl">选择企业资料</h3>
							<p class="mx-auto mt-2 max-w-sm text-xs leading-5 text-[var(--ink-muted)]">
								PDF、Office、OpenDocument、Markdown、CSV 与纯文本，单个文件不超过 10MB。
							</p>
							<Input
								type="file"
								bind:files={fileList}
								class="mt-5 rounded-lg border border-[var(--line)] bg-white px-3"
								accept=".pdf,.doc,.docx,.docm,.ppt,.pptx,.pptm,.xls,.xlsx,.xlsm,.xlsb,.odt,.ods,.odp,.rtf,.epub,.csv,.txt,.md,.json,.log,.html,.xml"
							/>
						</div>
						{#if indexProgress}
							<div
								class="rounded-xl border border-[var(--line)] bg-white/55 p-4"
								aria-live="polite"
							>
								<div class="flex items-center justify-between gap-3 text-xs">
									<span class="font-semibold">{indexProgress.message}</span>
									<span class="font-mono text-[var(--ink-muted)]">{indexProgress.percent}%</span>
								</div>
								<div
									class="mt-3 h-2 overflow-hidden rounded-full bg-[var(--paper-deep)]"
									role="progressbar"
									aria-label="文档索引进度"
									aria-valuemin="0"
									aria-valuemax="100"
									aria-valuenow={indexProgress.percent}
								>
									<div
										class="h-full rounded-full bg-[var(--signal)] transition-[width] duration-300"
										style:width={`${indexProgress.percent}%`}
									></div>
								</div>
								{#if indexProgress.total && indexProgress.completed !== undefined}
									<p class="mt-2 text-[10px] text-[var(--ink-faint)]">
										{indexProgress.completed} / {indexProgress.total} 个片段
									</p>
								{/if}
							</div>
						{/if}
						<Button onclick={upload} disabled={busy || !fileList?.length} class="rounded-lg">
							{#if busy}<LoaderCircleIcon class="animate-spin" />{/if} 上传并索引
						</Button>
					{:else}
						<form
							class="grid gap-5"
							onsubmit={(event) => {
								event.preventDefault();
								void importText();
							}}
						>
							<div class="grid gap-2">
								<Label for="import-title">资料标题</Label><Input
									id="import-title"
									bind:value={title}
									maxlength={200}
									placeholder="例如：2026 年员工休假制度"
									required
								/>
							</div>
							<div class="grid gap-2">
								<Label for="import-content">正文内容</Label><Textarea
									id="import-content"
									bind:value={content}
									class="min-h-72 rounded-xl border border-[var(--line)] bg-white/55 p-4 font-mono text-[13px] leading-6"
									placeholder="粘贴 Markdown 或纯文本……"
									required
								/>
							</div>
							<Button
								type="submit"
								disabled={busy || !title.trim() || !content.trim()}
								class="rounded-lg"
							>
								{#if busy}<LoaderCircleIcon class="animate-spin" />{:else}<BracesIcon />{/if} 导入并索引
							</Button>
						</form>
					{/if}
				</div>
			{/if}
		</div>
	</Sheet.Content>
</Sheet.Root>
