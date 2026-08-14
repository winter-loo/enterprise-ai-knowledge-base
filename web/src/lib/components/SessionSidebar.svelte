<script lang="ts">
	import ArchiveIcon from '@lucide/svelte/icons/archive';
	import MessageSquareTextIcon from '@lucide/svelte/icons/message-square-text';
	import MoreHorizontalIcon from '@lucide/svelte/icons/more-horizontal';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import SearchIcon from '@lucide/svelte/icons/search';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import type { LocalChatSession } from '$lib/chat/sessions';
	import { Button } from '$lib/components/ui/button';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
	import BrandMark from './BrandMark.svelte';

	let {
		sessions,
		activeId,
		onNew,
		onOpen,
		onDelete,
		onOpenKnowledge,
		onOpenQuickSearch
	}: {
		sessions: LocalChatSession[];
		activeId: string;
		onNew: () => void;
		onOpen: (session: LocalChatSession) => void;
		onDelete: (session: LocalChatSession) => void;
		onOpenKnowledge: () => void;
		onOpenQuickSearch: () => void;
	} = $props();

	function relativeTime(value: string): string {
		const elapsed = Date.now() - new Date(value).getTime();
		if (elapsed < 60_000) return '刚刚';
		if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)} 分钟前`;
		if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)} 小时前`;
		return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(
			new Date(value)
		);
	}
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="flex items-center gap-3 px-5 py-5">
		<span class="text-[var(--ink)]"><BrandMark size={34} /></span>
		<div>
			<div class="font-heading text-xl leading-none font-semibold">知屿</div>
			<div class="mt-1 text-[8px] font-bold tracking-[0.23em] text-[var(--ink-faint)] uppercase">
				Knowledge atelier
			</div>
		</div>
	</div>

	<div class="grid gap-2 px-4 pt-2">
		<Button onclick={onNew} class="h-11 justify-start rounded-xl px-4 tracking-normal normal-case">
			<PlusIcon class="size-4" /> 新建对话
		</Button>
		<div class="grid grid-cols-2 gap-2">
			<Button
				variant="outline"
				onclick={onOpenKnowledge}
				class="rounded-xl tracking-normal normal-case"><ArchiveIcon />资料</Button
			>
			<Button
				variant="outline"
				onclick={onOpenQuickSearch}
				class="rounded-xl tracking-normal normal-case"><SearchIcon />检索</Button
			>
		</div>
	</div>

	<div class="mt-7 flex items-center justify-between px-5">
		<span class="text-[9px] font-bold tracking-[0.18em] text-[var(--ink-faint)] uppercase"
			>最近研究</span
		>
		<span class="font-mono text-[10px] text-[var(--ink-faint)]">{sessions.length}</span>
	</div>

	<div class="mt-2 min-h-0 flex-1 overflow-y-auto px-3 pb-4">
		{#if sessions.length}
			<div class="grid gap-1">
				{#each sessions as session (session.id)}
					<div
						class:session-active={session.id === activeId}
						class="session-row group relative rounded-xl"
					>
						<button
							type="button"
							class="flex w-full min-w-0 items-start gap-3 px-3 py-3 pr-10 text-left"
							onclick={() => onOpen(session)}
						>
							<MessageSquareTextIcon class="mt-0.5 size-3.5 shrink-0 text-[var(--ink-faint)]" />
							<span class="min-w-0">
								<span class="block truncate text-xs font-medium">{session.title}</span>
								<span class="mt-1 block text-[10px] text-[var(--ink-faint)]"
									>{relativeTime(session.updatedAt)}</span
								>
							</span>
						</button>
						<DropdownMenu.Root>
							<DropdownMenu.Trigger>
								{#snippet child({ props })}
									<Button
										{...props}
										variant="ghost"
										size="icon-xs"
										class="absolute top-2.5 right-2 opacity-100 lg:opacity-0 lg:group-hover:opacity-100 lg:focus-visible:opacity-100"
										aria-label="对话操作"><MoreHorizontalIcon /></Button
									>
								{/snippet}
							</DropdownMenu.Trigger>
							<DropdownMenu.Content align="end">
								<DropdownMenu.Item variant="destructive" onclick={() => onDelete(session)}
									><Trash2Icon />删除会话</DropdownMenu.Item
								>
							</DropdownMenu.Content>
						</DropdownMenu.Root>
					</div>
				{/each}
			</div>
		{:else}
			<p class="px-3 py-8 text-center text-xs leading-5 text-[var(--ink-faint)]">
				你的研究会话会出现在这里
			</p>
		{/if}
	</div>

	<div class="border-t border-[var(--line)] px-5 py-4">
		<div class="flex items-center gap-2.5">
			<div
				class="grid size-8 place-items-center rounded-full bg-[var(--ink)] text-[10px] font-bold text-white"
			>
				AI
			</div>
			<div class="min-w-0">
				<div class="text-xs font-semibold">企业知识工作台</div>
				<div class="text-[10px] text-[var(--ink-faint)]">本机浏览器会话索引</div>
			</div>
		</div>
	</div>
</div>
