<script lang="ts">
	import BookOpenIcon from '@lucide/svelte/icons/book-open';
	import BriefcaseBusinessIcon from '@lucide/svelte/icons/briefcase-business';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import type { KnowledgeBase, Project } from '$lib/api/types';
	import { Button } from '$lib/components/ui/button';
	import * as Select from '$lib/components/ui/select';

	let {
		knowledgeBases,
		projects,
		kbId,
		projectId,
		disabled = false,
		onCreateKnowledgeBase,
		onCreateProject,
		onKnowledgeBaseChange,
		onProjectChange
	}: {
		knowledgeBases: KnowledgeBase[];
		projects: Project[];
		kbId: string;
		projectId: string;
		disabled?: boolean;
		onCreateKnowledgeBase: () => void;
		onCreateProject: () => void;
		onKnowledgeBaseChange: (value: string) => void;
		onProjectChange: (value: string) => void;
	} = $props();

	let kbLabel = $derived(knowledgeBases.find((kb) => kb.id === kbId)?.name ?? '选择知识库');
	let projectLabel = $derived(
		projects.find((project) => project.id === projectId)?.name ?? '选择项目'
	);
</script>

<div
	class="flex min-w-0 [scrollbar-width:none] items-center gap-1 overflow-x-auto border-t border-[var(--line)] px-1 py-1.5 [&::-webkit-scrollbar]:hidden"
	aria-label="当前资料范围"
>
	<div class="flex shrink-0 items-center rounded-lg bg-[var(--paper-deep)] pl-2">
		<BookOpenIcon class="size-3.5 shrink-0 text-[var(--ink-faint)]" aria-hidden="true" />
		<div class="flex items-center">
			<Select.Root type="single" value={kbId} onValueChange={onKnowledgeBaseChange} {disabled}>
				<Select.Trigger
					aria-label="选择知识库"
					class="h-8 max-w-40 min-w-0 border-b-0 px-1.5 py-0 text-[11px] font-semibold sm:max-w-52"
				>
					<span class="truncate">{kbLabel}</span>
				</Select.Trigger>
				<Select.Content>
					{#each knowledgeBases as kb (kb.id)}
						<Select.Item value={kb.id} label={kb.name} />
					{/each}
				</Select.Content>
			</Select.Root>
			<Button
				variant="ghost"
				size="icon-xs"
				class="mr-0.5 size-7 rounded-md text-[var(--ink-faint)]"
				onclick={onCreateKnowledgeBase}
				{disabled}
				aria-label="新建知识库"><PlusIcon /></Button
			>
		</div>
	</div>

	<div class="flex shrink-0 items-center rounded-lg bg-[var(--paper-deep)] pl-2">
		<BriefcaseBusinessIcon class="size-3.5 shrink-0 text-[var(--ink-faint)]" aria-hidden="true" />
		<div class="flex items-center">
			<Select.Root
				type="single"
				value={projectId}
				onValueChange={onProjectChange}
				disabled={disabled || !projects.length}
			>
				<Select.Trigger
					aria-label="选择项目范围"
					class="h-8 max-w-36 min-w-0 border-b-0 px-1.5 py-0 text-[11px] font-semibold sm:max-w-48"
				>
					<span class="truncate">{projectLabel}</span>
				</Select.Trigger>
				<Select.Content>
					{#each projects as project (project.id)}
						<Select.Item value={project.id} label={project.name} />
					{/each}
				</Select.Content>
			</Select.Root>
			<Button
				variant="ghost"
				size="icon-xs"
				class="mr-0.5 size-7 rounded-md text-[var(--ink-faint)]"
				onclick={onCreateProject}
				{disabled}
				aria-label="新建项目"><PlusIcon /></Button
			>
		</div>
	</div>
</div>
