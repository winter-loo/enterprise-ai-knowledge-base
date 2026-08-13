# Enterprise AI Knowledge Base

企业知识库 RAG MVP，按本地 Wiki 的共享 PostgreSQL evidence 表和 Project 范围实现。

- AnyDoc / pdf-inspector 文档解析
- PostgreSQL + pgvector 唯一存储路径
- Qwen3 Embedding（1024 维）
- 知识库内多个 Project
- `kb_id + project_id + department/general` 召回前过滤
- pgvector 语义分 + PostgreSQL 词法分 + freshness
- 可选 OpenAI-compatible 回答生成与本地证据 fallback
- 原生 HTML 前端

## 运行

```bash
cd /home/ldd/enterprise-ai-knowledge-base
uv sync --locked
set -a; source .local/dev.env; set +a
export DATABASE_URL='postgresql:///enterprise_ai_kb'
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010
```

默认 Embedding 服务：

```text
http://127.0.0.1:11434/v1
qwen3-embedding:0.6b
1024 dimensions
```

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

`document/import` 接收 `title`、`content`、`kb_id`、`project_id`、`department`。`chat/completions` 额外接收 `session_id`、`question`、`top_k`，以 SSE 返回 `sources`、`delta`、`done` 或 `error` JSON 事件。会话消息保存在 PostgreSQL `chat_messages`；生成时只读取最近 12 条，完整历史仍可查询和清除。

Redis 没有加入：现有 PostgreSQL 已同时满足向量持久化和会话存储，任务书只将 Redis 标为推荐。

上传和问答必须带同一个 `kb_id`、`project_id` 和可信身份推导出的 `department`。当前页面中的部门仍是演示输入，不是真实 SSO/RBAC。

## 数据模型

```text
knowledge_bases
  └─ projects
       └─ knowledge_evidence
```

所有可检索片段统一进入 `knowledge_evidence`，包含正文、embedding、来源、知识库、Project、部门范围和时间。Project 控制相关性边界；部门控制访问范围，两者不能互相替代。

## 测试

```bash
uv run pytest -q
uv run python -m compileall -q app tests
```

详细说明见 [`docs/运行原理与面试讲解.md`](docs/运行原理与面试讲解.md)。
