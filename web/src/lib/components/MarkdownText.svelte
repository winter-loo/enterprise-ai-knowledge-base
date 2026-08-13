<script lang="ts">
	import DOMPurify from 'dompurify';
	import { marked } from 'marked';

	let { content }: { content: string } = $props();
	let html = $derived(DOMPurify.sanitize(marked.parse(content, { async: false }) as string));
</script>

<div
	class="prose max-w-none text-[15px] leading-7 prose-stone dark:prose-invert prose-headings:font-heading prose-headings:font-medium prose-a:text-[var(--signal)] prose-strong:text-foreground prose-code:rounded prose-code:bg-stone-100 prose-code:px-1.5 prose-code:py-0.5 prose-code:text-[13px] prose-code:before:content-none prose-code:after:content-none"
>
	<!-- The parsed Markdown is sanitized by DOMPurify before it reaches this sink. -->
	<!-- eslint-disable-next-line svelte/no-at-html-tags -->
	{@html html}
</div>
