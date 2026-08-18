# Enterprise AI Knowledge Base

企业知识库 RAG。一个公司的知识保存在单一 `company` Knowledge Base 中；**Project 是最小的知识检索和授权边界**。

- `rag/`：文档入库、检索和有据回答；不保存对话。
- `session/`：服务端拥有的私人 Session 与消息历史。
- `authz/`：固定 Project Grant、平台管理员与 PostgreSQL RLS 策略。
- `web/`：Svelte 工作台；用户只会看到自己有 Grant 的 Project 和自己的历史。

## 授权模型

调用方以稳定的 **Principal** 身份请求。开发环境由 Vite 开发网关向全部 API 注入 `x-principal: admin`；浏览器不能选择 Principal。

```text
Principal ── Grant(role) ──> Project ──> documents / chunks
    │
    └── owns ────────────> private Session
```

- Viewer：读取、检索、问答并管理自己的 Session。
- Editor：Viewer 加文档上传和导入。
- Manager：Editor 加本 Project 的 Grant 管理；任何 Manager 都能创建 Project，并自动成为新 Project 的 Manager。
- Platform Administrator：管理和读取所有 Project 知识，但不能读取其他 Principal 的私人 Session。

每个 `(principal_id, project_id)` 只有一条 Grant。没有部门、群组、本地用户目录、自定义角色、访问 capability 或浏览器 session token。

RAG 和 Session 在每个数据库事务写入 `app.principal_id`。RLS 再次确认 Project Grant：知识读写需要相应角色；Session 则同时要求 owner 和仍有效的 Project Grant。因此撤权提交后，下一次查询立即失效。

## 运行

```bash
uv sync --locked
make check
set -a; source .local/dev.env; set +a
export DATABASE_URL='postgresql:///enterprise_ai_kb'
```

首次使用新模型，或要明确丢弃旧 scope 数据时，先停止服务并执行：

```bash
make reset-dev-data APP_ENV=development CONFIRM_RESET_DEV_DATA=yes
```

该命令会删除**当前开发数据库**中的知识、Grant 和 Session 数据；服务启动不会自动删除数据。

按顺序启动服务，使 Authz 先创建 Grant 表：

```bash
make dev-authz    # 8012
make dev-rag      # 8010
make dev-session  # 8011
make dev-web      # Vite；向所有 API 注入开发 Principal
```

服务都共享一个 `DATABASE_URL`。生产环境必须将服务置于独立 API Gateway 的私有网络之后；服务数据库账号不得拥有业务表、不得有 `BYPASSRLS`，以确保 `FORCE ROW LEVEL SECURITY` 有效。

生产由独立的 schema owner 运行一次 DDL 与 RLS 安装，运行时服务只使用低权限的 `DATABASE_URL`。启动时会拒绝 owner 或带 `BYPASSRLS` 的运行账号：

```bash
export APP_ENV=production
export DATABASE_OWNER_URL='postgresql://schema-owner:...@db/enterprise_ai_kb'
make provision-database
```

开发环境仍可由本地 `DATABASE_URL` 自动建表；`provision-database` 用于生产部署或需要与运行账号分离的环境。

默认 Embedding 服务：

```text
http://127.0.0.1:11434/v1
qwen3-embedding:0.6b
1024 dimensions
```

## API

所有业务 API 都需要可信网关注入的 `x-principal`。

```text
# RAG
GET  /api/projects                         # 仅返回已授权 Project
POST /api/projects                         # Manager 或平台管理员
GET  /api/documents?project_id=...
POST /api/documents/upload                 # Editor 或以上，NDJSON 进度流
POST /api/v1/document/import               # Editor 或以上
POST /api/retrieve                         # {question, project_id, top_k}
POST /api/ask                              # 加 history / stream 的有据回答
GET  /api/evidence/{id}?project_id=...

# Session
POST   /api/v1/chat/sessions               # {project_id}
GET    /api/v1/chat/sessions?project_id=...
POST   /api/v1/chat/completions            # {session_id, question, top_k}
GET    /api/v1/chat/history/{session_id}
DELETE /api/v1/chat/session/{session_id}

# Authz（第一阶段仅 API，不提供成员 UI）
POST   /api/v1/authz/authorize
GET    /api/v1/authz/projects/{project_id}/grants
PUT    /api/v1/authz/projects/{project_id}/grants
DELETE /api/v1/authz/projects/{project_id}/grants/{principal_id}
```

## 批量导入

CLI 也必须以某个 Principal 身份写入现有 Project：

```bash
uv run import-documents /path/to/docs --project-id project-123 --principal-id admin
uv run import-documents /path/to/docs --project-id project-123 --principal-id alice --ext md,pdf
```

## 未来接入 Supabase Auth

第一阶段不集成 Supabase。未来用独立 API Gateway 替换 Vite：它验证 Supabase JWT、移除客户端伪造的身份头，并以 JWT 的 `issuer + subject` 生成稳定 Principal ID。Supabase 的 access/refresh token 轮换只属于登录态；它不会再影响 Project、Grant 或 Session 身份。

详细决策见 [ADR 0005](docs/adr/0005-project-grants-with-principal-rls.md)。

## 开发检查

```bash
make lint
make typecheck
make test
make compile
make check
```
