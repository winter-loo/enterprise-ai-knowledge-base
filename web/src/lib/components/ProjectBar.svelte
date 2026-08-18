<script lang="ts">
	import BriefcaseBusinessIcon from '@lucide/svelte/icons/briefcase-business';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import type { Project } from '$lib/api/types';
	import { Button } from '$lib/components/ui/button';
	import * as Select from '$lib/components/ui/select';

	let {
		projects,
		projectId,
		disabled = false,
		onCreateProject,
		onProjectChange
	}: {
		projects: Project[];
		projectId: string;
		disabled?: boolean;
		onCreateProject: () => void;
		onProjectChange: (value: string) => void;
	} = $props();

	let projectLabel = $derived(
		projects.find((project) => project.id === projectId)?.name ?? '选择 Project'
	);
</script>

<div
	class="flex min-w-0 [scrollbar-width:none] items-center gap-1 overflow-x-auto border-t border-[var(--line)] px-1 py-1.5 [&::-webkit-scrollbar]:hidden"
	aria-label="当前 Project"
>
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
					aria-label="选择 Project"
					class="h-8 max-w-48 min-w-0 border-b-0 px-1.5 py-0 text-[11px] font-semibold"
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
				aria-label="新建 Project"><PlusIcon /></Button
			>
		</div>
	</div>
</div>
