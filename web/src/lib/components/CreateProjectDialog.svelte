<script lang="ts">
	import LoaderCircleIcon from '@lucide/svelte/icons/loader-circle';
	import { Button } from '$lib/components/ui/button';
	import * as Dialog from '$lib/components/ui/dialog';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Textarea } from '$lib/components/ui/textarea';

	let {
		open = $bindable(false),
		loading = false,
		onSubmit
	}: {
		open: boolean;
		loading?: boolean;
		onSubmit: (name: string, description: string) => Promise<boolean>;
	} = $props();

	let name = $state('');
	let description = $state('');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (!name.trim()) return;
		if (await onSubmit(name.trim(), description.trim())) {
			name = '';
			description = '';
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content class="rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-7 sm:max-w-lg">
		<Dialog.Header>
			<Dialog.Title class="font-heading text-2xl font-medium">新建 Project</Dialog.Title>
			<Dialog.Description class="leading-6">
				Project 是知识检索与授权的最小边界。创建后你会成为它的 Manager。
			</Dialog.Description>
		</Dialog.Header>
		<form class="grid gap-5" onsubmit={submit}>
			<div class="grid gap-2">
				<Label for="project-name">Project 名称</Label>
				<Input
					id="project-name"
					bind:value={name}
					maxlength={100}
					placeholder="例如：星河发布计划"
					required
				/>
			</div>
			<div class="grid gap-2">
				<Label for="project-description">说明（可选）</Label>
				<Textarea
					id="project-description"
					bind:value={description}
					maxlength={500}
					placeholder="帮助团队理解这组资料的用途和边界"
					class="min-h-24"
				/>
			</div>
			<Dialog.Footer>
				<Button type="submit" disabled={loading || !name.trim()} class="rounded-lg">
					{#if loading}<LoaderCircleIcon class="animate-spin" />{/if}
					创建
				</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>
