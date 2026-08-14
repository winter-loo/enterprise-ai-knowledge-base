# Enterprise AI Knowledge Base

企业知识库 RAG MVP，按本地 Wiki 的共享 PostgreSQL evidence 表和 Project 范围实现。

- AnyDoc / pdf-inspector 文档解析
- PostgreSQL + pgvector 唯一存储路径
- Qwen3 Embedding（1024 维）
- LightRAG-inspired `fixed`、`recursive`、`semantic`、`paragraph` 多切片策略
- 知识库内多个 Project
- `kb_id + project_id + department/general` 召回前过滤
- pgvector 语义分 + PostgreSQL 词法分 + freshness
- OpenAI-compatible 文档片段摘要、可选回答生成与本地证据 fallback
- SvelteKit 2 / Svelte 5 + shadcn-svelte 聊天界面
- Vercel AI SDK 客户端状态与流式回答

## 运行

首次安装依赖并启用提交前检查：

```bash
uv sync --locked
npm ci --prefix web
make install-hooks
```

本地开发使用两个终端。先启动 Python API：

```bash
set -a; source .local/dev.env; set +a
export DATABASE_URL='postgresql:///enterprise_ai_kb'
make dev-api
```

再启动 SvelteKit 开发服务器：

```bash
make dev-web
```

SvelteKit 开发服务器代理 `/api` 到 `http://127.0.0.1:8010`，因此浏览器不需要 CORS 配置。

生产运行时，先构建 SvelteKit adapter-static 产物，再由 FastAPI 同源托管：

```bash
make check
set -a; source .local/dev.env; set +a
export DATABASE_URL='postgresql:///enterprise_ai_kb'
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010
```

`make check` 会构建 `web/build`。如果直接启动 FastAPI 但该产物不存在，API 仍可使用，而 Web 入口会返回明确的 `503` 构建提示。`pre-commit` 与 GitHub Actions 都调用同一套检查。

## 应用架构

```text
浏览器
  ├─ /api/*  → FastAPI → PostgreSQL / pgvector / Embedding / LLM
  └─ 其他路径 → FastAPI → web/build (SvelteKit SPA)
```

`web/` 是独立的 npm 工程，负责 Svelte 5 界面、前端状态和 API 协议适配；`app/` 保持业务 API、文档解析、检索和会话持久化。生产环境只暴露 FastAPI 端口，不需要单独运行 Node 服务器。

默认 Embedding 服务：

```text
http://127.0.0.1:11434/v1
qwen3-embedding:0.6b
1024 dimensions
```

文档入库使用 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL` 调用 OpenAI-compatible `/responses`，为每个片段生成忠实摘要；未配置或单片摘要失败时，该片摘要留空且索引继续。问答生成复用相同配置，并解析 `response.output_text.delta` 与 `response.completed` 流式事件；`LLM_MODEL` 默认为 `gpt-4o-mini`。面向 Web UI 的 `/api/v1/chat/completions` 路径保持不变，它是本项目自己的兼容层，并不代表内部仍调用 Chat Completions API。

## API

```text
GET  /api/health
GET  /api/knowledge-bases
POST /api/knowledge-bases
GET  /api/projects?kb_id=company
POST /api/projects
POST /api/documents/upload
GET  /api/documents?kb_id=company
POST /api/ask

# 文本导入与持久会话接口
POST   /api/v1/document/import
POST   /api/v1/chat/completions
GET    /api/v1/chat/history/{sessionId}
DELETE /api/v1/chat/session/{sessionId}
```

`document/import` 接收 `title`、`content`、`kb_id`、`project_id`、`department` 和 `chunking_strategy`。上传接口同样支持 `chunking_strategy`：`fixed` 是重叠固定窗口，`recursive` 按段落/换行/标点递归拆分（默认），`semantic` 根据句向量相邻距离切换主题，`paragraph` 保留 Markdown 标题上下文。`chat/completions` 额外接收 `session_id`、浏览器生成的 `session_token`、`question` 和 `top_k`，以 SSE 返回 `sources`、`delta`、`done` 或 `error` JSON 事件。会话消息按 token hash 与 scope 保存在 PostgreSQL `chat_messages`；生成时只读取最近 12 条，完整历史仍可查询和清除。没有 LLM 配置时，流式聊天会自动返回本地证据 fallback。

Redis 没有加入：现有 PostgreSQL 已同时满足向量持久化和会话存储，任务书只将 Redis 标为推荐。

上传和问答必须带同一个 `kb_id`、`project_id` 和可信身份推导出的 `department`。当前页面中的部门仍是演示输入，不是真实 SSO/RBAC；`session_token` 只能降低会话 ID 泄露后的误读/误删风险，也不能替代用户认证。生产部署必须在 API 前接入可信身份、授权策略和审计。

## 数据模型

```text
knowledge_bases
  └─ projects
       └─ knowledge_evidence
```

所有可检索片段统一进入 `knowledge_evidence`，包含正文、embedding、来源、知识库、Project、部门范围和时间。Project 控制相关性边界；部门控制访问范围，两者不能互相替代。

## 开发检查

```bash
make lint       # Ruff + Prettier + ESLint
make typecheck  # BasedPyright strict + svelte-check
make test       # Pytest + Vitest
make compile    # Python 字节码编译检查
make build      # SvelteKit 生产构建
make check      # 运行以上全部检查并构建
make format     # Ruff + Prettier 自动修复与格式化
```

Python 规则集中在 `pyproject.toml`，Web 规则位于 `web/`。Make 根据 `web/package-lock.json` 跟踪已安装依赖，一次检查中只执行一次 `npm ci`。本地、pre-commit 和 CI 均复用 `make check`，避免检查结果漂移。
