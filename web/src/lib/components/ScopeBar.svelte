<script lang="ts">
	import BookOpenIcon from '@lucide/svelte/icons/book-open';
	import BriefcaseBusinessIcon from '@lucide/svelte/icons/briefcase-business';
	import ShieldCheckIcon from '@lucide/svelte/icons/shield-check';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import type { KnowledgeBase, Project } from '$lib/api/types';
	import { Button } from '$lib/components/ui/button';
	import * as Select from '$lib/components/ui/select';

	let {
		knowledgeBases,
		projects,
		kbId,
		projectId,
		department,
		disabled = false,
		onCreateKnowledgeBase,
		onCreateProject,
		onKnowledgeBaseChange,
		onProjectChange,
		onDepartmentChange
	}: {
		knowledgeBases: KnowledgeBase[];
		projects: Project[];
		kbId: string;
		projectId: string;
		department: string;
		disabled?: boolean;
		onCreateKnowledgeBase: () => void;
		onCreateProject: () => void;
		onKnowledgeBaseChange: (value: string) => void;
		onProjectChange: (value: string) => void;
		onDepartmentChange: (value: string) => void;
	} = $props();

	const departments = [
		{ value: 'general', label: '通用资料' },
		{ value: 'engineering', label: '研发' },
		{ value: 'hr', label: '人事' },
		{ value: 'sales', label: '销售' }
	];

	let kbLabel = $derived(knowledgeBases.find((kb) => kb.id === kbId)?.name ?? '选择知识库');
	let projectLabel = $derived(
		projects.find((project) => project.id === projectId)?.name ?? '选择项目'
	);
	let departmentLabel = $derived(
		departments.find((item) => item.value === department)?.label ?? department
	);
</script>

<div
	class="scope-bar flex min-w-0 items-stretch overflow-x-auto border-b border-[var(--line)] bg-[color-mix(in_oklab,var(--paper)_88%,transparent)] px-4 backdrop-blur-xl lg:px-8"
>
	<div class="scope-field group min-w-[12rem] flex-1 py-3 pr-5">
		<div
			class="mb-0.5 flex items-center gap-1.5 text-[9px] font-bold tracking-[0.16em] text-[var(--ink-faint)] uppercase"
		>
			<BookOpenIcon class="size-3" /> 知识库
		</div>
		<div class="flex items-center gap-1">
			<Select.Root type="single" value={kbId} onValueChange={onKnowledgeBaseChange} {disabled}>
				<Select.Trigger class="h-7 min-w-0 flex-1 border-b-0 py-0 text-[13px] font-semibold">
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
				onclick={onCreateKnowledgeBase}
				{disabled}
				aria-label="新建知识库"><PlusIcon /></Button
			>
		</div>
	</div>

	<div class="scope-field min-w-[12rem] flex-1 border-l border-[var(--line)] px-5 py-3">
		<div
			class="mb-0.5 flex items-center gap-1.5 text-[9px] font-bold tracking-[0.16em] text-[var(--ink-faint)] uppercase"
		>
			<BriefcaseBusinessIcon class="size-3" /> 项目范围
		</div>
		<div class="flex items-center gap-1">
			<Select.Root
				type="single"
				value={projectId}
				onValueChange={onProjectChange}
				disabled={disabled || !projects.length}
			>
				<Select.Trigger class="h-7 min-w-0 flex-1 border-b-0 py-0 text-[13px] font-semibold">
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
				onclick={onCreateProject}
				{disabled}
				aria-label="新建项目"><PlusIcon /></Button
			>
		</div>
	</div>

	<div class="scope-field min-w-[10rem] border-l border-[var(--line)] py-3 pl-5">
		<div
			class="mb-0.5 flex items-center gap-1.5 text-[9px] font-bold tracking-[0.16em] text-[var(--ink-faint)] uppercase"
		>
			<ShieldCheckIcon class="size-3" /> 资料范围 · 演示
		</div>
		<Select.Root type="single" value={department} onValueChange={onDepartmentChange} {disabled}>
			<Select.Trigger class="h-7 min-w-36 border-b-0 py-0 text-[13px] font-semibold">
				<span>{departmentLabel}</span>
			</Select.Trigger>
			<Select.Content>
				{#each departments as item (item.value)}
					<Select.Item value={item.value} label={item.label} />
				{/each}
			</Select.Content>
		</Select.Root>
	</div>
</div>
