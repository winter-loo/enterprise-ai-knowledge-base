<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Chat } from '@ai-sdk/svelte';
	import ArchiveIcon from '@lucide/svelte/icons/archive';
	import ArrowUpIcon from '@lucide/svelte/icons/arrow-up';
	import CircleStopIcon from '@lucide/svelte/icons/circle-stop';
	import MenuIcon from '@lucide/svelte/icons/menu';
	import PaperclipIcon from '@lucide/svelte/icons/paperclip';
	import SearchIcon from '@lucide/svelte/icons/search';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';
	import { toast } from 'svelte-sonner';
	import { rag, session as sessionApi } from '$lib/api/client';
	import type {
		ChatHistoryResponse,
		ChatPromptMessage,
		ChatSession,
		DocumentRecord,
		Project,
		ProjectReference
	} from '$lib/api/types';
	import { PythonSseChatTransport, type PythonChatMessage } from '$lib/ai/python-sse-transport';
	import BrandMark from '$lib/components/BrandMark.svelte';
	import CreateProjectDialog from '$lib/components/CreateProjectDialog.svelte';
	import KnowledgeDrawer from '$lib/components/KnowledgeDrawer.svelte';
	import MessageBubble from '$lib/components/MessageBubble.svelte';
	import QuickSearchDialog from '$lib/components/QuickSearchDialog.svelte';
	import ProjectBar from '$lib/components/ProjectBar.svelte';
	import SessionSidebar from '$lib/components/SessionSidebar.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Sheet from '$lib/components/ui/sheet';
	import { Textarea } from '$lib/components/ui/textarea';

	const transport = new PythonSseChatTransport();
	const LAST_PROJECT_KEY = 'enterprise-kb.last-project';
	const prompts = [
		{ title: '制度问答', text: '请总结当前 Project 中最重要的制度与注意事项。' },
		{ title: '项目脉络', text: '根据现有资料，梳理这个 Project 的关键里程碑与风险。' },
		{ title: '查找差异', text: '现有文档之间是否有相互矛盾或需要澄清的内容？' }
	];

	let projects = $state<Project[]>([]);
	let documents = $state<DocumentRecord[]>([]);
	let sessions = $state<ChatSession[]>([]);
	let projectId = $state('');
	let activeSession = $state<ChatSession | null>(null);
	let input = $state('');
	let topK = $state(5);
	let ready = $state(false);
	let loadingProject = $state(false);
	let serviceState = $state<'checking' | 'online' | 'offline'>('checking');
	let knowledgeOpen = $state(false);
	let quickSearchOpen = $state(false);
	let createOpen = $state(false);
	let createLoading = $state(false);
	let mobileNavOpen = $state(false);
	let messagesViewport = $state<HTMLElement>();
	let incompleteMessageIds = $state<string[]>([]);
	let chat = $state(createChat('placeholder', []));

	let project = $derived<ProjectReference>({ projectId });
	let activeProject = $derived(projects.find((item) => item.id === projectId));
	let isGenerating = $derived(chat.status === 'submitted' || chat.status === 'streaming');
	let visibleDocumentCount = $derived(documents.length);
	let quickSearchHistory = $derived(
		chat.messages
			.filter((message) => message.role === 'user' || message.role === 'assistant')
			.map((message) => ({
				role: message.role,
				content: message.parts
					.filter((part) => part.type === 'text')
					.map((part) => part.text)
					.join('\n')
			})) as ChatPromptMessage[]
	);

	function createChat(sessionId: string, messages: PythonChatMessage[]): Chat<PythonChatMessage> {
		return new Chat<PythonChatMessage>({
			id: sessionId,
			messages,
			transport,
			onFinish() {
				void refreshSessions();
			},
			onError(error) {
				toast.error(error.message);
			}
		});
	}

	function historyToMessages(history: ChatHistoryResponse): PythonChatMessage[] {
		return history.messages.map((message, index) => ({
			id: `${history.session_id}-${index}`,
			role: message.role,
			parts: [{ type: 'text', text: message.content }]
		})) as PythonChatMessage[];
	}

	function setActiveSession(next: ChatSession | null, messages: PythonChatMessage[] = []): void {
		incompleteMessageIds = [];
		activeSession = next;
		chat = createChat(next?.id ?? `project-${projectId || 'none'}`, messages);
	}

	async function refreshSessions(): Promise<void> {
		if (!projectId) return;
		sessions = await sessionApi.listSessions(projectId);
		const currentSession = activeSession;
		if (currentSession && !sessions.some((item) => item.id === currentSession.id)) {
			setActiveSession(null);
		}
	}

	async function refreshDocuments(): Promise<void> {
		if (!projectId) return;
		documents = await rag.listDocuments({ project_id: projectId });
	}

	async function changeProject(nextProjectId: string, force = false): Promise<void> {
		if (!nextProjectId || (!force && nextProjectId === projectId)) return;
		if (isGenerating) await chat.stop();
		loadingProject = true;
		try {
			const [nextDocuments, nextSessions] = await Promise.all([
				rag.listDocuments({ project_id: nextProjectId }),
				sessionApi.listSessions(nextProjectId)
			]);
			projectId = nextProjectId;
			window.localStorage.setItem(LAST_PROJECT_KEY, nextProjectId);
			documents = nextDocuments;
			sessions = nextSessions;
			if (nextSessions[0]) await openSession(nextSessions[0], true);
			else setActiveSession(null);
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '无法切换 Project');
		} finally {
			loadingProject = false;
		}
	}

	async function initialize(): Promise<void> {
		ready = false;
		serviceState = 'checking';
		try {
			const [ragHealth, sessionHealth, nextProjects] = await Promise.all([
				rag.health(),
				sessionApi.health(),
				rag.listProjects()
			]);
			projects = nextProjects;
			serviceState =
				ragHealth.status === 'ok' && sessionHealth.status === 'ok' ? 'online' : 'offline';
			const remembered = window.localStorage.getItem(LAST_PROJECT_KEY);
			const firstProject = nextProjects.find((item) => item.id === remembered) ?? nextProjects[0];
			ready = true;
			if (firstProject) await changeProject(firstProject.id, true);
		} catch (error) {
			serviceState = 'offline';
			toast.error(error instanceof Error ? error.message : '无法连接知识库服务');
		}
	}

	async function newSession(): Promise<ChatSession | null> {
		if (!projectId || loadingProject) return null;
		try {
			const created = await sessionApi.createSession({ project_id: projectId });
			sessions = [created, ...sessions];
			setActiveSession(created);
			return created;
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '无法创建会话');
			return null;
		}
	}

	async function openSession(next: ChatSession, alreadyCurrentProject = false): Promise<void> {
		if (!alreadyCurrentProject && next.id === activeSession?.id) {
			mobileNavOpen = false;
			return;
		}
		if (isGenerating) await chat.stop();
		try {
			const history = await sessionApi.getHistory(next.id);
			setActiveSession(next, historyToMessages(history));
			mobileNavOpen = false;
		} catch (error) {
			if (error instanceof Error) toast.warning(error.message);
			await refreshSessions();
		}
	}

	async function deleteSession(next: ChatSession): Promise<void> {
		if (!window.confirm(`删除“${next.title}”及其服务器消息？此操作无法撤销。`)) return;
		try {
			if (next.id === activeSession?.id && isGenerating) await chat.stop();
			await sessionApi.clearSession(next.id);
			sessions = sessions.filter((item) => item.id !== next.id);
			if (next.id === activeSession?.id) setActiveSession(null);
			toast.success('会话已删除');
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '删除会话失败');
		}
	}

	async function send(text = input): Promise<void> {
		const question = text.trim();
		if (!question || !ready || serviceState !== 'online' || loadingProject || isGenerating) return;
		const session = activeSession ?? (await newSession());
		if (!session) return;
		input = '';
		if (session.title === '新的研究') {
			const title = question.replace(/\s+/g, ' ').slice(0, 36);
			activeSession = { ...session, title };
			sessions = sessions.map((item) => (item.id === session.id ? { ...item, title } : item));
		}
		await chat.sendMessage({ text: question }, { body: { session_id: session.id, top_k: topK } });
	}

	function composerKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
			event.preventDefault();
			void send();
		}
	}

	async function createProject(name: string, description: string): Promise<boolean> {
		createLoading = true;
		try {
			const created = await rag.createProject({ name, description });
			projects = [...projects, created];
			createOpen = false;
			await changeProject(created.id, true);
			toast.success('Project 已创建');
			return true;
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '创建 Project 失败');
			return false;
		} finally {
			createLoading = false;
		}
	}

	onMount(() => {
		void initialize();
	});

	$effect(() => {
		const messageCount = chat.messages.length;
		void tick().then(() => {
			if (messageCount > 0 && messagesViewport)
				messagesViewport.scrollTop = messagesViewport.scrollHeight;
		});
	});
</script>

<svelte:head>
	<title>知屿 · 企业知识助手</title>
	<meta name="description" content="以 Project 为最小权限边界的企业知识问答工作台" />
</svelte:head>

<div class="app-frame h-dvh overflow-hidden bg-[var(--paper)] text-[var(--ink)]">
	<aside class="hidden min-h-0 border-r border-[var(--line)] bg-[var(--paper-deep)] lg:block">
		<SessionSidebar
			{sessions}
			activeId={activeSession?.id ?? ''}
			onNew={() => void newSession()}
			onOpen={(item) => void openSession(item)}
			onDelete={(item) => void deleteSession(item)}
			onOpenKnowledge={() => (knowledgeOpen = true)}
			onOpenQuickSearch={() => (quickSearchOpen = true)}
		/>
	</aside>

	<main class="relative flex min-h-0 min-w-0 flex-col">
		<header
			class="flex h-[4.2rem] shrink-0 items-center gap-3 border-b border-[var(--line)] bg-[color-mix(in_oklab,var(--paper)_90%,transparent)] px-4 backdrop-blur-xl lg:px-8"
		>
			<Button
				variant="ghost"
				size="icon-sm"
				class="lg:hidden"
				onclick={() => (mobileNavOpen = true)}
				aria-label="打开导航"><MenuIcon /></Button
			>
			<div class="min-w-0 flex-1">
				<div class="flex min-w-0 items-center gap-2">
					<h1 class="truncate font-heading text-lg font-medium sm:text-xl">
						{activeSession?.title ?? activeProject?.name ?? '选择 Project'}
					</h1>
					<span class="hidden size-1 rounded-full bg-[var(--line-strong)] sm:block"></span>
					<span
						class="hidden truncate text-[10px] font-bold tracking-[0.14em] text-[var(--ink-faint)] uppercase sm:block"
						>{activeProject?.name ?? '未选择'}</span
					>
				</div>
				<p class="mt-0.5 truncate text-[10px] text-[var(--ink-faint)]">
					公司知识库 · {visibleDocumentCount} 份当前 Project 资料
				</p>
			</div>
			<div class="flex items-center gap-2">
				<div class="mr-1 hidden items-center gap-2 text-[10px] text-[var(--ink-faint)] sm:flex">
					<span
						class:status-online={serviceState === 'online'}
						class:status-offline={serviceState === 'offline'}
						class="status-dot"
					></span>{serviceState === 'online'
						? '服务在线'
						: serviceState === 'offline'
							? '连接异常'
							: '检查中'}
				</div>
				<Button
					variant="ghost"
					size="icon-sm"
					onclick={() => (quickSearchOpen = true)}
					aria-label="快速检索"
					disabled={!projectId}><SearchIcon /></Button
				>
				<Button
					variant="outline"
					class="hidden rounded-lg tracking-normal normal-case sm:inline-flex"
					onclick={() => (knowledgeOpen = true)}
					disabled={!projectId}><ArchiveIcon />知识资料</Button
				>
			</div>
		</header>

		<section
			bind:this={messagesViewport}
			class="conversation-scroll min-h-0 flex-1 overflow-y-auto scroll-smooth"
		>
			<div class="mx-auto flex min-h-full w-full max-w-4xl flex-col px-4 py-8 sm:px-8 sm:py-10">
				{#if !ready || loadingProject}
					<div class="grid flex-1 place-items-center">
						<div class="text-center">
							<div class="knowledge-loader mx-auto mb-4"></div>
							<p class="text-xs tracking-[0.12em] text-[var(--ink-faint)] uppercase">
								正在加载 Project
							</p>
						</div>
					</div>
				{:else if !projectId}
					<div class="my-auto text-center">
						<BrandMark size={48} />
						<h2 class="mt-5 font-heading text-3xl">还没有可访问的 Project</h2>
						<p class="mt-3 text-sm text-[var(--ink-muted)]">
							Manager 或平台管理员可以创建第一个 Project。
						</p>
						<Button class="mt-6 rounded-xl" onclick={() => (createOpen = true)}>新建 Project</Button
						>
					</div>
				{:else if chat.messages.length === 0}
					<div class="empty-enter my-auto py-8 sm:py-14">
						<div class="mb-6 flex items-center gap-3">
							<span class="h-px w-12 bg-[var(--signal)]"></span><span
								class="text-[10px] font-bold tracking-[0.2em] text-[var(--signal)] uppercase"
								>Project grounded intelligence</span
							>
						</div>
						<h2
							class="max-w-2xl font-heading text-4xl leading-[1.06] font-medium tracking-[-0.025em] text-balance sm:text-6xl"
						>
							让企业知识<br /><em class="font-normal text-[var(--signal)]">有据可循。</em>
						</h2>
						<p class="mt-5 max-w-xl text-sm leading-7 text-[var(--ink-muted)] sm:text-base">
							当前 Project 是一个独立的检索、知识和权限边界。你的会话只对自己可见。
						</p>
						<div class="mt-9 grid gap-2 sm:grid-cols-3">
							{#each prompts as prompt, index (prompt.title)}<button
									type="button"
									class="prompt-card group rounded-2xl border border-[var(--line)] bg-white/45 p-4 text-left"
									disabled={serviceState !== 'online'}
									onclick={() => void send(prompt.text)}
									><div class="mb-5 flex items-center justify-between">
										<span class="font-mono text-[10px] text-[var(--ink-faint)]">0{index + 1}</span
										><SparklesIcon
											class="size-3.5 text-[var(--signal)] opacity-0 transition group-hover:opacity-100"
										/>
									</div>
									<div class="text-xs font-bold tracking-[0.08em]">{prompt.title}</div>
									<p class="mt-2 text-xs leading-5 text-[var(--ink-muted)]">
										{prompt.text}
									</p></button
								>{/each}
						</div>
					</div>
				{:else}
					<div class="grid gap-8 pb-5">
						{#each chat.messages as message, index (message.id)}<MessageBubble
								{message}
								{project}
								incomplete={(chat.status === 'error' &&
									index === chat.messages.length - 1 &&
									message.role === 'assistant') ||
									incompleteMessageIds.includes(message.id)}
								streaming={isGenerating &&
									index === chat.messages.length - 1 &&
									message.role === 'assistant'}
							/>{/each}
					</div>
				{/if}
			</div>
		</section>

		<footer class="composer-wrap shrink-0 px-3 pb-3 sm:px-6 sm:pb-5">
			<div class="mx-auto max-w-4xl">
				<div
					class="composer-shell rounded-[1.4rem] border border-[var(--line-strong)] bg-[color-mix(in_oklab,var(--paper)_90%,white)] p-2 shadow-[0_16px_60px_rgba(35,48,44,.10)] backdrop-blur-xl focus-within:border-[var(--signal)]"
				>
					<Textarea
						bind:value={input}
						onkeydown={composerKeydown}
						maxlength={2000}
						disabled={!ready || serviceState !== 'online' || loadingProject || !projectId}
						placeholder="询问当前 Project 内的任何问题……"
						class="max-h-40 min-h-12 border-0 px-3 py-2.5 text-[15px] leading-6 focus-visible:border-0"
					/><ProjectBar
						{projects}
						{projectId}
						disabled={!ready || loadingProject || isGenerating || createLoading}
						onCreateProject={() => (createOpen = true)}
						onProjectChange={(value) => void changeProject(value)}
					/>
					<div class="flex items-center gap-1.5 px-1 pb-1">
						<Button
							variant="ghost"
							size="icon-sm"
							onclick={() => (knowledgeOpen = true)}
							disabled={!projectId}
							aria-label="上传知识资料"><PaperclipIcon /></Button
						><label
							class="ml-1 hidden items-center gap-2 text-[10px] text-[var(--ink-faint)] sm:flex"
							>召回 <input
								type="range"
								min="1"
								max="10"
								bind:value={topK}
								class="w-20 accent-[var(--signal)]"
							/><span class="w-3 font-mono">{topK}</span></label
						><span class="ml-auto hidden text-[10px] text-[var(--ink-faint)] sm:block"
							>Enter 发送 · Shift + Enter 换行</span
						>{#if isGenerating}<Button
								size="icon-sm"
								variant="outline"
								class="ml-2 rounded-xl"
								onclick={() => void chat.stop()}
								aria-label="停止生成"><CircleStopIcon /></Button
							>{:else}<Button
								size="icon-sm"
								class="ml-2 rounded-xl"
								onclick={() => void send()}
								disabled={!input.trim() ||
									!projectId ||
									!ready ||
									serviceState !== 'online' ||
									loadingProject}
								aria-label="发送问题"><ArrowUpIcon /></Button
							>{/if}
					</div>
				</div>
				<p class="mt-2 text-center text-[9px] tracking-[0.04em] text-[var(--ink-faint)]">
					回答仅基于当前 Project 的已索引资料；重要决策请核对原文。
				</p>
			</div>
		</footer>
	</main>
</div>

<Sheet.Root bind:open={mobileNavOpen}>
	<Sheet.Content
		side="left"
		class="w-[min(88vw,19rem)] border-[var(--line)] bg-[var(--paper-deep)] p-0"
		><Sheet.Title class="sr-only">会话导航</Sheet.Title><Sheet.Description class="sr-only"
			>打开历史研究或新建会话</Sheet.Description
		><SessionSidebar
			{sessions}
			activeId={activeSession?.id ?? ''}
			onNew={() => {
				void newSession();
				mobileNavOpen = false;
			}}
			onOpen={(item) => void openSession(item)}
			onDelete={(item) => void deleteSession(item)}
			onOpenKnowledge={() => {
				knowledgeOpen = true;
				mobileNavOpen = false;
			}}
			onOpenQuickSearch={() => {
				quickSearchOpen = true;
				mobileNavOpen = false;
			}}
		/></Sheet.Content
	>
</Sheet.Root>

<KnowledgeDrawer bind:open={knowledgeOpen} {documents} {project} onImported={refreshDocuments} />
<QuickSearchDialog bind:open={quickSearchOpen} {project} history={quickSearchHistory} />
<CreateProjectDialog bind:open={createOpen} loading={createLoading} onSubmit={createProject} />
