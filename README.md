# Enterprise AI Knowledge Base

企业知识库 RAG MVP，按本地 Wiki 的共享 PostgreSQL evidence 表和 Project 范围实现。

- AnyDoc / pdf-inspector 文档解析
- PostgreSQL + pgvector 唯一存储路径
- Qwen3 Embedding（1024 维）
- LightRAG-inspired `fixed`、`recursive`、`semantic`、`paragraph` 多切片策略
- 知识库内多个 Project
- `kb_id + project_id + department/general` 召回前过滤
- pgvector 语义分 + PostgreSQL 词法分 + freshness
- OpenAI-compatible 文档片段摘要与可选回答生成
- 原生 HTML 前端

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
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010
```

`pre-commit` 会在每次提交前自动运行同一套检查；GitHub Actions 也会在 push 和 pull request 时执行，避免跳过本地 hook 后合入问题。

默认 Embedding 服务：

```text
http://127.0.0.1:11434/v1
qwen3-embedding:0.6b
1024 dimensions
```

文档入库使用 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL` 调用 OpenAI-compatible `/chat/completions`，为每个片段生成忠实摘要；未配置或单片摘要失败时，该片摘要留空且索引继续。问答生成复用相同配置，`LLM_MODEL` 默认为 `gpt-4o-mini`。

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

# 任务书要求的兼容接口
POST   /api/v1/document/import
POST   /api/v1/chat/completions
GET    /api/v1/chat/history/{sessionId}
DELETE /api/v1/chat/session/{sessionId}
```

`document/import` 接收 `title`、`content`、`kb_id`、`project_id`、`department` 和 `chunking_strategy`。上传接口同样支持 `chunking_strategy`：`fixed` 是重叠固定窗口，`recursive` 按段落/换行/标点递归拆分（默认），`semantic` 根据句向量相邻距离切换主题，`paragraph` 通过 Multimark AST 识别 Markdown 标题并保留章节上下文。`chat/completions` 额外接收 `session_id`、`question`、`top_k`，以 SSE 返回 `sources`、`delta`、`done` 或 `error` JSON 事件。会话消息保存在 PostgreSQL `chat_messages`；生成时只读取最近 12 条，完整历史仍可查询和清除。

Redis 没有加入：现有 PostgreSQL 已同时满足向量持久化和会话存储，任务书只将 Redis 标为推荐。

上传和问答必须带同一个 `kb_id`、`project_id` 和可信身份推导出的 `department`。当前页面中的部门仍是演示输入，不是真实 SSO/RBAC。

## 数据模型

```text
knowledge_bases
  └─ projects
       └─ knowledge_evidence
```

所有可检索片段统一进入 `knowledge_evidence`，包含正文、embedding、来源、知识库、Project、部门范围和时间。Project 控制相关性边界；部门控制访问范围，两者不能互相替代。

## 开发检查

```bash
make lint       # Ruff 静态检查
make typecheck  # BasedPyright strict 类型检查
make test       # Pytest
make compile    # Python 字节码编译检查
make check      # 依次运行以上全部检查
make format     # 自动修复 Ruff 问题并格式化
```

Ruff 和 BasedPyright 的规则集中配置在 `pyproject.toml`。Ruff 检查并验证格式化整个仓库；BasedPyright 以 strict 模式检查生产代码 `app/`；测试代码由 Ruff 和 Pytest 覆盖。本地、pre-commit 和 CI 都复用这些配置与 `make check`，避免检查结果不一致。
