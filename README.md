# Enterprise AI Knowledge Base

企业知识库 RAG 服务，按本地 Wiki 的共享 PostgreSQL evidence 表和 Project 范围实现。RAG 检索/问答与会话管理已拆成两个可独立部署的服务：

- `rag/` — RAG 服务：文档入库、检索、有据回答。**无状态**，会话历史由调用方内联传入，不落会话状态。
- `session/` — 会话服务：记住对话历史，作为 RAG HTTP API 的客户端（和外部 Agent 平级）。
- `web/` — SvelteKit 前端，聊天走会话服务，知识管理/快速搜索直连 RAG 服务。

## 能力

- AnyDoc / pdf-inspector 文档解析
- PostgreSQL + pgvector 唯一存储路径
- Qwen3 Embedding（1024 维）
- LightRAG-inspired `fixed`、`recursive`、`semantic`、`paragraph` 多切片策略
- 知识库内多个 Project
- `kb_id + project_id + department/general` 召回前过滤
- pgvector 语义分 + PostgreSQL 词法分 + freshness
- OpenAI-compatible 文档片段摘要与可选回答生成
- `retrieve`（检索原语）+ `ask`（有据回答原语，支持流式）

## 运行

首次安装依赖并启用提交前检查：

```bash
uv sync --locked
make install-hooks
```

运行服务前先执行完整检查：

```bash
make check
set -a; source .local/dev.env; set +a
export DATABASE_URL='postgresql:///enterprise_ai_kb'
```

分别启动两个服务（RAG 用 8010，会话服务用 8011）：

```bash
uv run uvicorn rag.main:app --host 127.0.0.1 --port 8010
uv run uvicorn session.main:app --host 127.0.0.1 --port 8011
```

或使用 Makefile 目标：

```bash
make dev-rag      # 8010
make dev-session  # 8011
make dev-web      # Vite 开发服务器（/api/v1/chat/* 代理到 8011，其余 /api 代理到 8010）
```

会话服务通过 `RAG_BASE_URL` 定位 RAG 服务，默认 `http://127.0.0.1:8010`。两个服务共享同一个 `DATABASE_URL`（同一 PostgreSQL，各自只触碰自己的表）。

默认 Embedding 服务：

```text
http://127.0.0.1:11434/v1
qwen3-embedding:0.6b
1024 dimensions
```

文档入库使用 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL` 调用 OpenAI-compatible `/chat/completions`，为每个片段生成忠实摘要；未配置或单片摘要失败时，该片摘要留空且索引继续。问答生成复用相同配置，`LLM_MODEL` 默认为 `gpt-4o-mini`。

## 命令行批量导入

把一个目录下的文档批量写入知识库：

```bash
# 递归导入目录下所有可解析文件
uv run python -m rag.cli /path/to/docs

# 只导入指定后缀的文件
uv run python -m rag.cli /path/to/docs --ext md,txt,pdf

# 指定范围与切片策略
uv run python -m rag.cli /path/to/docs --kb-id company --project-id default \
  --department general --chunking-strategy recursive
```

安装依赖后也可用控制台命令：

```bash
uv run import-documents /path/to/docs --ext md,pdf
```

参数：`directory` 为要扫描的目录（递归读取其中所有文件）；`--ext` 为逗号分隔的后缀列表，缺省处理所有可解析文件；`--kb-id`、`--project-id`、`--department`、`--chunking-strategy` 与上传接口一致，默认分别为 `company`、`default`、`general`、`recursive`。批量导入是尽力而为的：单个文件解析或写入失败会跳过并继续，结束后打印「导入 / 跳过 / 失败」统计，任一文件失败时退出码为 1。

## API

RAG 服务：

```text
GET  /api/health
GET  /api/knowledge-bases
POST /api/knowledge-bases
GET  /api/projects?kb_id=company
POST /api/projects
POST /api/documents/upload
GET  /api/documents?kb_id=company
POST /api/retrieve              # 检索原语，返回完整 content 片段
POST /api/ask                   # 有据回答原语；stream=true 时 SSE 返回 sources/delta/done/error
POST /api/v1/document/import    # 任务书兼容接口
```

会话服务：

```text
GET    /api/health
POST   /api/v1/chat/completions              # 组合 RAG 流式 ask 后转发
GET    /api/v1/chat/history/{sessionId}
DELETE /api/v1/chat/session/{sessionId}
```

`retrieve` 接收 `question`、`kb_id`、`project_id`、`department`、`top_k`，返回 `{chunks:[{id,filename,chunk_index,score,content,summary}], retrieved}`。`ask` 额外接收 `history`（内联 `[{role, content}]`）和 `stream`；非流式返回 `{answer, answer_mode, citations, retrieved}`。`chat/completions` 接收 `session_id`、`session_token`、`question` 及 scope，以 SSE 返回 `sources`、`delta`、`done` 或 `error` JSON 事件；会话消息保存在 PostgreSQL `chat_messages`，生成时只读取最近 12 条。

`document/import` 接收 `title`、`content`、`kb_id`、`project_id`、`department` 和 `chunking_strategy`。上传接口同样支持 `chunking_strategy`：`fixed` 是重叠固定窗口，`recursive` 按段落/换行/标点递归拆分（默认），`semantic` 根据句向量相邻距离切换主题，`paragraph` 通过 Multimark AST 识别 Markdown 标题并保留章节上下文。

Redis 没有加入：现有 PostgreSQL 已同时满足向量持久化和会话存储，任务书只将 Redis 标为推荐。

## 数据模型

```text
rag 服务拥有：      knowledge_bases → projects → knowledge_evidence
session 服务拥有：  chat_messages, chat_session_tombstones
```

所有可检索片段统一进入 `knowledge_evidence`，包含正文、embedding、来源、知识库、Project、部门范围和时间。Project 控制相关性边界；部门控制访问范围，两者不能互相替代。

RAG 服务信任网络边界，不内建鉴权：`kb_id/project_id/department` 由调用方明文传入，真实 SSO/RBAC 属于外层网关或调用方职责（当前页面中的部门仍是演示输入）。

## 开发检查

```bash
make lint       # Ruff + Web lint
make typecheck  # BasedPyright strict + svelte-check
make test       # Pytest + Vitest
make compile    # Python 字节码编译检查
make check      # 依次运行以上全部检查
make format     # 自动修复 Ruff 问题并格式化
```

Ruff 和 BasedPyright 的规则集中配置在 `pyproject.toml`。Ruff 检查并验证格式化整个仓库；BasedPyright 以 strict 模式检查生产代码 `rag/` 与 `session/`；测试代码由 Ruff 和 Pytest 覆盖。本地、pre-commit 和 CI 都复用这些配置与 `make check`，避免检查结果不一致。
