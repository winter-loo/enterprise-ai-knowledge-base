<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Chat } from '@ai-sdk/svelte';
	import { toast } from 'svelte-sonner';
	import ArrowUpIcon from '@lucide/svelte/icons/arrow-up';
	import ArchiveIcon from '@lucide/svelte/icons/archive';
	import BookOpenCheckIcon from '@lucide/svelte/icons/book-open-check';
	import CircleStopIcon from '@lucide/svelte/icons/circle-stop';
	import MenuIcon from '@lucide/svelte/icons/menu';
	import PaperclipIcon from '@lucide/svelte/icons/paperclip';
	import SearchIcon from '@lucide/svelte/icons/search';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';
	import type { DocumentRecord, KnowledgeBase, Project } from '$lib/api/types';
	import { rag, session as sessionApi } from '$lib/api/client';
	import { PythonSseChatTransport, type PythonChatMessage } from '$lib/ai/python-sse-transport';
	import {
		createLocalSession,
		historyToUIMessages,
		isChatSessionStorageKey,
		readActiveSessionId,
		readSessions,
		removeStoredSession,
		titleFromQuestion,
		uiMessagesToPromptHistory,
		writeActiveSessionId,
		writeSession,
		type LocalChatSession
	} from '$lib/chat/sessions';
	import { shouldStartNewSession, type ChatScope } from '$lib/chat/scope-policy';
	import { Button } from '$lib/components/ui/button';
	import * as Sheet from '$lib/components/ui/sheet';
	import { Textarea } from '$lib/components/ui/textarea';
	import CreateScopeDialog from '$lib/components/CreateScopeDialog.svelte';
	import KnowledgeDrawer from '$lib/components/KnowledgeDrawer.svelte';
	import MessageBubble from '$lib/components/MessageBubble.svelte';
	import QuickSearchDialog from '$lib/components/QuickSearchDialog.svelte';
	import ScopeBar from '$lib/components/ScopeBar.svelte';
	import SessionSidebar from '$lib/components/SessionSidebar.svelte';

	const transport = new PythonSseChatTransport();
	const emptyScope: ChatScope = { kbId: '', projectId: '', department: 'general' };
	const placeholderSession: LocalChatSession = {
		id: 'initial',
		token: '',
		title: '新的研究',
		createdAt: '',
		updatedAt: '',
		scope: emptyScope
	};

	let knowledgeBases = $state<KnowledgeBase[]>([]);
	let projects = $state<Project[]>([]);
	let documents = $state<DocumentRecord[]>([]);
	let sessions = $state<LocalChatSession[]>([]);
	let activeSession = $state<LocalChatSession>(placeholderSession);
	let kbId = $state('');
	let projectId = $state('');
	let department = $state('general');
	let input = $state('');
	let topK = $state(5);
	let ready = $state(false);
	let serviceState = $state<'checking' | 'online' | 'offline'>('checking');
	let loadingScope = $state(false);
	let knowledgeOpen = $state(false);
	let quickSearchOpen = $state(false);
	let mobileNavOpen = $state(false);
	let createKind = $state<'knowledge-base' | 'project'>('knowledge-base');
	let createOpen = $state(false);
	let createLoading = $state(false);
	let messagesViewport = $state<HTMLElement>();
	let chat = $state(createChat(placeholderSession.id, []));
	let scopeRequestId = 0;
	let scopeController: AbortController | undefined;
	let incompleteMessageIds = $state<string[]>([]);

	let scope: ChatScope = $derived({ kbId, projectId, department });
	let quickSearchHistory = $derived(uiMessagesToPromptHistory(chat.messages));
	let isGenerating = $derived(chat.status === 'submitted' || chat.status === 'streaming');
	let activeKnowledgeBase = $derived(knowledgeBases.find((item) => item.id === kbId));
	let activeProject = $derived(projects.find((item) => item.id === projectId));
	let visibleDocumentCount = $derived(
		documents.filter(
			(document) =>
				document.project_id === projectId &&
				(document.department === department || document.department === 'general')
		).length
	);

	const prompts = [
		{ title: '制度核对', text: '请总结当前项目中最重要的制度要求，并标注依据。' },
		{ title: '项目脉络', text: '根据现有资料，梳理这个项目的关键里程碑与风险。' },
		{ title: '查找差异', text: '现有文档之间是否有相互矛盾或需要澄清的内容？' }
	];

	function createChat(sessionId: string, messages: PythonChatMessage[]): Chat<PythonChatMessage> {
		return new Chat<PythonChatMessage>({
			id: sessionId,
			messages,
			transport,
			onFinish({ message, isAbort, isError }) {
				if ((isAbort || isError) && message.parts.some((part) => part.type === 'text')) {
					incompleteMessageIds = [...new Set([...incompleteMessageIds, message.id])];
				}
			},
			onError(error) {
				toast.error(error.message);
			}
		});
	}

	function apiScope(
		value: ChatScope,
		sessionToken: string
	): {
		kb_id: string;
		project_id: string;
		department: string;
		session_token: string;
	} {
		return {
			kb_id: value.kbId,
			project_id: value.projectId,
			department: value.department,
			session_token: sessionToken
		};
	}

	function persistSession(next: LocalChatSession): boolean {
		if (!writeSession(next)) return false;
		sessions = readSessions();
		return true;
	}

	function setActiveSession(session: LocalChatSession, messages: PythonChatMessage[] = []): void {
		incompleteMessageIds = [];
		activeSession = session;
		chat = createChat(session.id, messages);
		writeActiveSessionId(session.id);
	}

	function beginScopeRequest(): { id: number; signal: AbortSignal } {
		scopeController?.abort();
		scopeController = new AbortController();
		scopeRequestId += 1;
		loadingScope = true;
		return { id: scopeRequestId, signal: scopeController.signal };
	}

	function finishScopeRequest(id: number): void {
		if (id !== scopeRequestId) return;
		loadingScope = false;
		scopeController = undefined;
	}

	function cancelScopeRequest(): void {
		scopeController?.abort();
		scopeController = undefined;
		scopeRequestId += 1;
		loadingScope = false;
	}

	function isAbortError(error: unknown): boolean {
		return error instanceof Error && error.name === 'AbortError';
	}

	function newSession(nextScope: ChatScope = scope): boolean {
		if (!nextScope.kbId || !nextScope.projectId) return false;
		cancelScopeRequest();
		if (isGenerating) void chat.stop();
		let session: LocalChatSession;
		try {
			session = createLocalSession(nextScope);
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '无法安全创建会话');
			return false;
		}
		if (!persistSession(session)) {
			toast.error('浏览器无法保存会话凭证，请释放本地存储空间后重试');
			return false;
		}
		setActiveSession(session);
		input = '';
		return true;
	}

	async function initialize(): Promise<void> {
		ready = false;
		serviceState = 'checking';
		try {
			const [health, sessionHealth, kbs] = await Promise.all([
				rag.health(),
				sessionApi.health(),
				rag.listKnowledgeBases()
			]);
			const nextSessions = readSessions();
			let remembered = nextSessions.find((session) => session.id === readActiveSessionId());
			const nextKbId = kbs.some((kb) => kb.id === remembered?.scope.kbId)
				? remembered!.scope.kbId
				: (kbs[0]?.id ?? '');
			if (!nextKbId) throw new Error('服务端没有可用的知识库');

			const [nextProjects, nextDocuments] = await Promise.all([
				rag.listProjects(nextKbId),
				rag.listDocuments(nextKbId)
			]);
			const nextProjectId = nextProjects.some(
				(project) => project.id === remembered?.scope.projectId
			)
				? remembered!.scope.projectId
				: (nextProjects[0]?.id ?? '');
			if (!nextProjectId) throw new Error('服务端没有可用的项目范围');
			const nextDepartment = remembered?.scope.department || 'general';

			const resolvedScope = {
				kbId: nextKbId,
				projectId: nextProjectId,
				department: nextDepartment
			};
			let restoredMessages: PythonChatMessage[] | undefined;
			if (remembered && !shouldStartNewSession(remembered.scope, resolvedScope)) {
				try {
					const history = await sessionApi.getHistory(
						remembered.id,
						apiScope(remembered.scope, remembered.token)
					);
					restoredMessages = historyToUIMessages(history.messages) as PythonChatMessage[];
				} catch {
					remembered = undefined;
					toast.warning('原会话无法恢复，已在相同资料范围中新建会话');
				}
			}
			knowledgeBases = kbs;
			projects = nextProjects;
			documents = nextDocuments;
			sessions = nextSessions;
			kbId = nextKbId;
			projectId = nextProjectId;
			department = nextDepartment;
			if (remembered && restoredMessages) setActiveSession(remembered, restoredMessages);
			else if (!newSession(resolvedScope)) throw new Error('无法持久化安全会话，工作台未启用');
			serviceState = health.status === 'ok' && sessionHealth.status === 'ok' ? 'online' : 'offline';
			ready = true;
		} catch (error) {
			serviceState = 'offline';
			toast.error(error instanceof Error ? error.message : '无法连接知识库服务');
		}
	}

	onMount(() => {
		const syncSessions = (event: StorageEvent) => {
			if (!isChatSessionStorageKey(event.key)) return;
			const nextSessions = readSessions();
			sessions = nextSessions;
			const storedActive = nextSessions.find((session) => session.id === activeSession.id);
			if (storedActive) {
				activeSession = storedActive;
			} else if (activeSession.id !== placeholderSession.id) {
				if (isGenerating) void chat.stop();
				if (!newSession(scope)) {
					setActiveSession(placeholderSession);
					ready = false;
					serviceState = 'offline';
				}
			}
		};
		window.addEventListener('storage', syncSessions);
		void initialize();
		return () => {
			window.removeEventListener('storage', syncSessions);
			scopeController?.abort();
		};
	});

	$effect(() => {
		const messageCount = chat.messages.length;
		const scrollTrigger = `${messageCount}:${chat.status}`;
		void tick().then(() => {
			if (scrollTrigger && messageCount > 0 && messagesViewport) {
				messagesViewport.scrollTop = messagesViewport.scrollHeight;
			}
		});
	});

	async function refreshDocuments(): Promise<void> {
		if (kbId) documents = await rag.listDocuments(kbId);
	}

	async function changeKnowledgeBase(value: string): Promise<void> {
		if (!value || value === activeSession.scope.kbId) return;
		const request = beginScopeRequest();
		try {
			const [nextProjects, nextDocuments] = await Promise.all([
				rag.listProjects(value, { signal: request.signal }),
				rag.listDocuments(value, { signal: request.signal })
			]);
			if (request.id !== scopeRequestId) return;
			const nextProjectId = nextProjects[0]?.id ?? '';
			if (!nextProjectId) throw new Error('该知识库没有可用项目');
			if (!newSession({ kbId: value, projectId: nextProjectId, department })) return;
			kbId = value;
			projects = nextProjects;
			projectId = nextProjectId;
			documents = nextDocuments;
		} catch (error) {
			if (request.id === scopeRequestId && !isAbortError(error)) {
				toast.error(error instanceof Error ? error.message : '无法切换知识库');
			}
		} finally {
			finishScopeRequest(request.id);
		}
	}

	function changeProject(value: string): void {
		if (!value || value === activeSession.scope.projectId) return;
		if (!newSession({ kbId, projectId: value, department })) return;
		projectId = value;
	}

	function changeDepartment(value: string): void {
		if (!value || value === activeSession.scope.department) return;
		if (!newSession({ kbId, projectId, department: value })) return;
		department = value;
	}

	async function openSession(session: LocalChatSession): Promise<void> {
		if (session.id === activeSession.id) {
			mobileNavOpen = false;
			return;
		}
		if (isGenerating) await chat.stop();
		const request = beginScopeRequest();
		try {
			const [nextProjects, nextDocuments, history] = await Promise.all([
				rag.listProjects(session.scope.kbId, { signal: request.signal }),
				rag.listDocuments(session.scope.kbId, { signal: request.signal }),
				sessionApi.getHistory(session.id, apiScope(session.scope, session.token), {
					signal: request.signal
				})
			]);
			if (request.id !== scopeRequestId) return;
			const nextProjectId = nextProjects.some((project) => project.id === session.scope.projectId)
				? session.scope.projectId
				: (nextProjects[0]?.id ?? '');
			if (!nextProjectId) throw new Error('该知识库没有可用项目');
			if (nextProjectId !== session.scope.projectId) {
				if (
					!newSession({
						kbId: session.scope.kbId,
						projectId: nextProjectId,
						department: session.scope.department
					})
				)
					return;
				toast.info('原项目已不可用，已为当前范围新建会话');
			} else {
				setActiveSession(session, historyToUIMessages(history.messages) as PythonChatMessage[]);
			}
			kbId = session.scope.kbId;
			projectId = nextProjectId;
			department = session.scope.department;
			projects = nextProjects;
			documents = nextDocuments;
			mobileNavOpen = false;
		} catch (error) {
			if (request.id === scopeRequestId && !isAbortError(error)) {
				toast.error(error instanceof Error ? error.message : '无法恢复会话');
			}
		} finally {
			finishScopeRequest(request.id);
		}
	}

	async function deleteSession(session: LocalChatSession): Promise<void> {
		if (!window.confirm(`删除“${session.title}”及其服务器消息？此操作无法撤销。`)) return;
		try {
			cancelScopeRequest();
			if (session.id === activeSession.id && isGenerating) await chat.stop();
			await sessionApi.clearSession(session.id, apiScope(session.scope, session.token));
			if (!removeStoredSession(session.id)) {
				toast.warning('服务器会话已删除，但浏览器未能更新本地列表；刷新后可再次清理条目');
				return;
			}
			const remaining = readSessions();
			sessions = remaining;
			if (session.id === activeSession.id) {
				const next = remaining[0];
				if (next) await openSession(next);
				else if (!newSession()) {
					setActiveSession(placeholderSession);
					ready = false;
					serviceState = 'offline';
				}
			}
			toast.success('会话已删除');
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '删除会话失败');
		}
	}

	async function send(text = input): Promise<void> {
		const question = text.trim();
		if (
			!question ||
			!ready ||
			serviceState !== 'online' ||
			loadingScope ||
			isGenerating ||
			!projectId ||
			activeSession.id === placeholderSession.id ||
			shouldStartNewSession(activeSession.scope, scope)
		)
			return;
		input = '';
		const now = new Date().toISOString();
		const updated = {
			...activeSession,
			title: activeSession.title === '新的研究' ? titleFromQuestion(question) : activeSession.title,
			updatedAt: now
		};
		activeSession = updated;
		if (!persistSession(updated)) {
			toast.error('无法保存会话状态，消息尚未发送');
			return;
		}
		await chat.sendMessage(
			{ text: question },
			{
				body: {
					session_id: activeSession.id,
					session_token: activeSession.token,
					kb_id: kbId,
					project_id: projectId,
					department,
					top_k: topK
				}
			}
		);
	}

	function composerKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
			event.preventDefault();
			void send();
		}
	}

	function openCreate(kind: 'knowledge-base' | 'project'): void {
		createKind = kind;
		createOpen = true;
	}

	async function createScope(name: string, description: string): Promise<boolean> {
		createLoading = true;
		let createdScope: ChatScope;
		try {
			if (createKind === 'knowledge-base') {
				const created = await rag.createKnowledgeBase({ name, description });
				createdScope = {
					kbId: created.id,
					projectId: created.default_project_id,
					department
				};
			} else {
				const created = await rag.createProject({ kb_id: kbId, name, description });
				createdScope = { kbId, projectId: created.id, department };
			}
		} catch (error) {
			toast.error(error instanceof Error ? error.message : '创建失败');
			createLoading = false;
			return false;
		}

		createOpen = false;
		const optimisticProject: Project = {
			id: createdScope.projectId,
			kb_id: createdScope.kbId,
			name: createKind === 'knowledge-base' ? '默认项目' : name,
			description: createKind === 'knowledge-base' ? '默认项目范围' : description,
			created_at: new Date().toISOString()
		};
		if (createKind === 'knowledge-base') {
			knowledgeBases = [
				...knowledgeBases,
				{
					id: createdScope.kbId,
					name,
					description,
					created_at: new Date().toISOString()
				}
			];
		} else if (!projects.some((project) => project.id === createdScope.projectId)) {
			projects = [...projects, optimisticProject];
		}
		if (!newSession(createdScope)) {
			toast.warning(
				`${createKind === 'knowledge-base' ? '知识库' : '项目'}已创建，但当前范围未切换`
			);
			createLoading = false;
			return true;
		}
		toast.success(`${createKind === 'knowledge-base' ? '知识库' : '项目'}已创建`);
		kbId = createdScope.kbId;
		projectId = createdScope.projectId;
		projects =
			createKind === 'knowledge-base'
				? [optimisticProject]
				: projects.some((project) => project.id === createdScope.projectId)
					? projects
					: [...projects, optimisticProject];
		if (createKind === 'knowledge-base') documents = [];
		const refreshSessionId = activeSession.id;
		try {
			const [nextKbs, nextProjects] = await Promise.all([
				rag.listKnowledgeBases(),
				rag.listProjects(createdScope.kbId)
			]);
			knowledgeBases = nextKbs;
			if (activeSession.id === refreshSessionId && kbId === createdScope.kbId) {
				projects = nextProjects;
			}
		} catch (error) {
			toast.warning(
				error instanceof Error
					? `已创建，但刷新失败：${error.message}`
					: '已创建，但范围列表刷新失败'
			);
		} finally {
			createLoading = false;
		}
		return true;
	}
</script>

<svelte:head>
	<title>知屿 · 企业知识助手</title>
	<meta name="description" content="可追溯、可控范围的企业 AI 知识问答工作台" />
</svelte:head>

<div class="app-frame h-dvh overflow-hidden bg-[var(--paper)] text-[var(--ink)]">
	<aside class="hidden min-h-0 border-r border-[var(--line)] bg-[var(--paper-deep)] lg:block">
		<SessionSidebar
			{sessions}
			activeId={activeSession.id}
			onNew={() => newSession()}
			onOpen={(session) => void openSession(session)}
			onDelete={(session) => void deleteSession(session)}
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
						{activeSession.title}
					</h1>
					<span class="hidden size-1 rounded-full bg-[var(--line-strong)] sm:block"></span>
					<span
						class="hidden truncate text-[10px] font-bold tracking-[0.14em] text-[var(--ink-faint)] uppercase sm:block"
						>{activeProject?.name ?? '加载范围'}</span
					>
				</div>
				<p class="mt-0.5 truncate text-[10px] text-[var(--ink-faint)]">
					{activeKnowledgeBase?.name ?? '企业知识工作台'} · {visibleDocumentCount} 份可见资料
				</p>
			</div>
			<div class="flex items-center gap-2">
				<div
					class="mr-1 hidden items-center gap-2 text-[10px] text-[var(--ink-faint)] sm:flex"
					title="健康接口仅表示 API 服务存活"
				>
					<span
						class:status-online={serviceState === 'online'}
						class:status-offline={serviceState === 'offline'}
						class="status-dot"
					></span>
					{serviceState === 'online'
						? '服务在线'
						: serviceState === 'offline'
							? '连接异常'
							: '检查中'}
				</div>
				<Button
					variant="ghost"
					size="icon-sm"
					onclick={() => (quickSearchOpen = true)}
					aria-label="快速检索"><SearchIcon /></Button
				>
				<Button
					variant="outline"
					class="hidden rounded-lg tracking-normal normal-case sm:inline-flex"
					onclick={() => (knowledgeOpen = true)}><ArchiveIcon />知识资料</Button
				>
			</div>
		</header>

		<ScopeBar
			{knowledgeBases}
			{projects}
			{kbId}
			{projectId}
			{department}
			disabled={!ready || loadingScope || isGenerating || createLoading}
			onCreateKnowledgeBase={() => openCreate('knowledge-base')}
			onCreateProject={() => openCreate('project')}
			onKnowledgeBaseChange={(value) => void changeKnowledgeBase(value)}
			onProjectChange={changeProject}
			onDepartmentChange={changeDepartment}
		/>

		<section
			bind:this={messagesViewport}
			class="conversation-scroll min-h-0 flex-1 overflow-y-auto scroll-smooth"
		>
			<div class="mx-auto flex min-h-full w-full max-w-4xl flex-col px-4 py-8 sm:px-8 sm:py-10">
				{#if !ready || loadingScope}
					<div class="grid flex-1 place-items-center">
						<div class="text-center">
							<div class="knowledge-loader mx-auto mb-4"></div>
							<p class="text-xs tracking-[0.12em] text-[var(--ink-faint)] uppercase">
								正在校准知识范围
							</p>
						</div>
					</div>
				{:else if chat.messages.length === 0}
					<div class="empty-enter my-auto py-8 sm:py-14">
						<div class="mb-6 flex items-center gap-3">
							<span class="h-px w-12 bg-[var(--signal)]"></span><span
								class="text-[10px] font-bold tracking-[0.2em] text-[var(--signal)] uppercase"
								>Grounded intelligence</span
							>
						</div>
						<h2
							class="max-w-2xl font-heading text-4xl leading-[1.06] font-medium tracking-[-0.025em] text-balance sm:text-6xl"
						>
							让企业知识<br /><em class="font-normal text-[var(--signal)]">有据可循。</em>
						</h2>
						<p class="mt-5 max-w-xl text-sm leading-7 text-[var(--ink-muted)] sm:text-base">
							在当前选择的资料范围内检索、交叉核对，并把每个结论连接回原始证据。
						</p>
						<div class="mt-9 grid gap-2 sm:grid-cols-3">
							{#each prompts as prompt, index (prompt.title)}
								<button
									type="button"
									class="prompt-card group rounded-2xl border border-[var(--line)] bg-white/45 p-4 text-left"
									disabled={!ready || serviceState !== 'online' || loadingScope}
									onclick={() => void send(prompt.text)}
								>
									<div class="mb-5 flex items-center justify-between">
										<span class="font-mono text-[10px] text-[var(--ink-faint)]">0{index + 1}</span
										><SparklesIcon
											class="size-3.5 text-[var(--signal)] opacity-0 transition group-hover:opacity-100"
										/>
									</div>
									<div class="text-xs font-bold tracking-[0.08em]">{prompt.title}</div>
									<p class="mt-2 text-xs leading-5 text-[var(--ink-muted)]">{prompt.text}</p>
								</button>
							{/each}
						</div>
					</div>
				{:else}
					<div class="grid gap-8 pb-5">
						{#each chat.messages as message, index (message.id)}
							<MessageBubble
								{message}
								{scope}
								incomplete={(chat.status === 'error' &&
									index === chat.messages.length - 1 &&
									message.role === 'assistant') ||
									incompleteMessageIds.includes(message.id)}
								streaming={isGenerating &&
									index === chat.messages.length - 1 &&
									message.role === 'assistant'}
							/>
						{/each}
						{#if chat.status === 'submitted'}
							<div class="message-enter grid grid-cols-[36px_minmax(0,1fr)] gap-4">
								<div
									class="grid size-9 place-items-center rounded-[10px] bg-[var(--ink)] text-[var(--paper)]"
								>
									<BookOpenCheckIcon class="size-4" />
								</div>
								<div>
									<div
										class="text-[11px] font-bold tracking-[0.16em] text-[var(--ink-muted)] uppercase"
									>
										知识助手
									</div>
									<div class="mt-3 flex gap-1.5">
										<span class="thinking-bar"></span><span class="thinking-bar"></span><span
											class="thinking-bar"
										></span>
									</div>
								</div>
							</div>
						{/if}
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
						disabled={!ready || serviceState !== 'online' || loadingScope}
						placeholder="询问当前知识范围内的任何问题……"
						class="max-h-40 min-h-12 border-0 px-3 py-2.5 text-[15px] leading-6 focus-visible:border-0"
					/>
					<div class="flex items-center gap-1.5 px-1 pb-1">
						<Button
							variant="ghost"
							size="icon-sm"
							onclick={() => (knowledgeOpen = true)}
							aria-label="上传知识资料"><PaperclipIcon /></Button
						>
						<label
							class="ml-1 hidden items-center gap-2 text-[10px] text-[var(--ink-faint)] sm:flex"
							>召回 <input
								type="range"
								min="1"
								max="10"
								bind:value={topK}
								class="w-20 accent-[var(--signal)]"
							/><span class="w-3 font-mono">{topK}</span></label
						>
						<span class="ml-auto hidden text-[10px] text-[var(--ink-faint)] sm:block"
							>Enter 发送 · Shift + Enter 换行</span
						>
						{#if isGenerating}
							<Button
								size="icon-sm"
								variant="outline"
								class="ml-2 rounded-xl"
								onclick={() => void chat.stop()}
								aria-label="停止生成"><CircleStopIcon /></Button
							>
						{:else}
							<Button
								size="icon-sm"
								class="ml-2 rounded-xl"
								onclick={() => void send()}
								disabled={!input.trim() ||
									!projectId ||
									!ready ||
									serviceState !== 'online' ||
									loadingScope}
								aria-label="发送问题"><ArrowUpIcon /></Button
							>
						{/if}
					</div>
				</div>
				<p class="mt-2 text-center text-[9px] tracking-[0.04em] text-[var(--ink-faint)]">
					回答仅基于当前所选范围内的已索引资料；重要决策请核对原文。
				</p>
			</div>
		</footer>
	</main>
</div>

<Sheet.Root bind:open={mobileNavOpen}>
	<Sheet.Content
		side="left"
		class="w-[min(88vw,19rem)] border-[var(--line)] bg-[var(--paper-deep)] p-0"
	>
		<Sheet.Title class="sr-only">会话导航</Sheet.Title>
		<Sheet.Description class="sr-only">打开历史研究或新建会话</Sheet.Description>
		<SessionSidebar
			{sessions}
			activeId={activeSession.id}
			onNew={() => {
				newSession();
				mobileNavOpen = false;
			}}
			onOpen={(session) => void openSession(session)}
			onDelete={(session) => void deleteSession(session)}
			onOpenKnowledge={() => {
				knowledgeOpen = true;
				mobileNavOpen = false;
			}}
			onOpenQuickSearch={() => {
				quickSearchOpen = true;
				mobileNavOpen = false;
			}}
		/>
	</Sheet.Content>
</Sheet.Root>

<KnowledgeDrawer bind:open={knowledgeOpen} {documents} {scope} onImported={refreshDocuments} />
<QuickSearchDialog bind:open={quickSearchOpen} {scope} history={quickSearchHistory} />
<CreateScopeDialog
	bind:open={createOpen}
	kind={createKind}
	loading={createLoading}
	onSubmit={createScope}
/>
