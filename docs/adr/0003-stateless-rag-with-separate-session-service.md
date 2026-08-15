---
status: accepted
---

# RAG is stateless; conversation state lives in a separate session service

The knowledge base is split into two independently deployable services: a RAG service that ingests documents and answers queries, and a session service that owns conversation history. The RAG service's two primitives — Retrieve and Ask — are stateless: conversation History is passed inline with each Ask request and never persisted by RAG. The session service is a client of the RAG HTTP API, exactly like an external AI agent, and adds only session credentials, per-turn persistence, and the clear-vs-stream race handling. This split lets the RAG capability be reused by the team's own session management or by external agents (Codex, Claude, etc.) without carrying any chat-state baggage.

## Considered Options

- Keep one service with RAG and session code separated only at module level. Rejected: the RAG capability could not be reached over HTTP by external agents without also deploying the session layer.
- Make the RAG service stateful (persist conversation history server-side). Rejected: external agents already have their own conversation loop, so server-side history would duplicate it and couple the RAG to a specific chat product.
- Extract session management to a separate module but call RAG in-process. Rejected: the RAG HTTP contract would go unverified by the repo's own session consumer, and the two could not scale or be replaced independently.
- Stateless RAG service plus a separate session service that composes it over HTTP. Accepted.

## Consequences

- The four task-book compatibility endpoints are preserved: `POST /api/v1/document/import` stays on the RAG service; `POST /api/v1/chat/completions`, `GET /api/v1/chat/history/{sessionId}`, and `DELETE /api/v1/chat/session/{sessionId}` stay on the session service.
- RAG owns `knowledge_bases`, `projects`, and `knowledge_evidence`; session owns `chat_messages` and `chat_session_tombstones`. They share one PostgreSQL database but touch disjoint tables.
- The RAG service is network-trusted: it does not authenticate callers, and Scope fields are passed as plaintext. Real SSO/RBAC is out of scope and belongs to an outer gateway or the caller.
- History size handed to Ask is the session service's responsibility; RAG does not remember anything between calls.
