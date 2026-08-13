<script lang="ts">
	import LoaderCircleIcon from '@lucide/svelte/icons/loader-circle';
	import { Button } from '$lib/components/ui/button';
	import * as Dialog from '$lib/components/ui/dialog';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Textarea } from '$lib/components/ui/textarea';

	let {
		open = $bindable(false),
		kind,
		loading = false,
		onSubmit
	}: {
		open: boolean;
		kind: 'knowledge-base' | 'project';
		loading?: boolean;
		onSubmit: (name: string, description: string) => Promise<boolean>;
	} = $props();

	let name = $state('');
	let description = $state('');
	let isKnowledgeBase = $derived(kind === 'knowledge-base');

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
			<Dialog.Title class="font-heading text-2xl font-medium"
				>新建{isKnowledgeBase ? '知识库' : '项目'}</Dialog.Title
			>
			<Dialog.Description class="leading-6">
				{isKnowledgeBase
					? '创建一套独立的企业知识边界，并自动生成默认项目。'
					: '在当前知识库内创建更聚焦的检索范围。'}
			</Dialog.Description>
		</Dialog.Header>
		<form class="grid gap-5" onsubmit={submit}>
			<div class="grid gap-2">
				<Label for="scope-name">名称</Label>
				<Input
					id="scope-name"
					bind:value={name}
					maxlength={100}
					placeholder={isKnowledgeBase ? '例如：公司制度中心' : '例如：星河发布计划'}
					required
				/>
			</div>
			<div class="grid gap-2">
				<Label for="scope-description">说明（可选）</Label>
				<Textarea
					id="scope-description"
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
