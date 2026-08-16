---
status: accepted
---

# Authz as a standalone service with resource-scoped RBAC

The knowledge base has no permission model today: access is a data-tagging convention, not authorization. This ADR replaces the plaintext `(kb_id, project_id, department)` Scope triple with a `Principal` identity carried by a trusted header, a resource-scoped RBAC grant model, a first-class department tree, and a standalone deployable `authz` service that owns all authorization state, logic, and the `knowledge_evidence` Row-Level Security policy. RAG becomes a permission-free content engine: it applies an opaque scope context to the database and lets Postgres enforce row visibility.

## Context

- **No identity.** No `users`/`roles`/`grants` tables exist anywhere in the repo; the only credential is the session capability token, which proves possession of a session, not identity. The README states the RAG service "trusts the network boundary, does not build in authentication" and that "SSO/RBAC belongs to an outer gateway or the caller; the department selector on the current page is still a demo input."
- **`department` is a free-form label, not an access check.** There is no `departments` table and no membership relation. The web `ScopeBar` hardcodes a four-value department dropdown; any caller can pass any string. The SQL predicate `(e.department=%s OR e.department='general')` in `store.search` and `store.get_evidence` is label matching — it cannot answer "does this caller belong to engineering?".
- **`project` has no membership.** `GET /api/projects` returns every project to any caller; any caller can retrieve against any project.
- **Enforcement is scattered.** The same scope predicate is duplicated in `search` and `get_evidence`; `ensure_kb`/`ensure_project` repeat per endpoint; the session service calls back into RAG's `/api/scope/resolve` over HTTP just to canonicalize `project_id`.
- **Magic values.** `project_id="default"` silently resolves to the oldest project (`ORDER BY created_at LIMIT 1`), and `department="general"` bypasses every check.
- **Single-valued department label.** A chunk can carry only one department, so it cannot express multi-department visibility or organizational-tree inheritance.

The root cause is that three distinct concerns were fused into one triple: **identity** (who is calling), **authorization** (what may they access), and **resource organization** (how data is arranged: kb → project → department). Only the third existed, and it was misread as the first two.

## Decisions

**D1 — Identity enters as a trusted `Principal`.** The edge (gateway/proxy) authenticates callers and injects an `x-principal` header (e.g. an SSO subject or service id). authz resolves and validates it. Client-supplied `x-principal` values are ignored at the edge. Future SSO/OIDC integration plugs into this same seam without changing callers.

**D2 — Authorization model is resource-scoped RBAC.** `users` and `roles` plus `grants` that bind `(principal, role, project?, department?)`. A grant answers both "can this principal perform action X" (endpoint level) and "which projects/departments may this principal see" (data level). Coarse control comes from roles; fine-grained control comes from the scope binding on each grant. An `authorize(principal, action, resource)` check guards endpoints; a `visible_scope(principal, kb_id, project_id)` result supplies the opaque scope_context that RLS enforces.

**D3 — Departments become a first-class tree.** A `departments` table with `parent_id`; visibility inherits downward (a member of a parent department may see its subtree). `general` is replaced by a real shared department ("公共" / public) instead of a magic string, so it is subject to the same membership rules as every other department.

**D4 — authz is a standalone deployable service.** `authz/` runs as its own FastAPI service (port 8012, `uv run uvicorn authz.main:app`), owning `users`, `roles`, `grants`, and `departments` tables. It shares the same PostgreSQL database as rag and session but touches disjoint tables — the established pattern in this repo. This is the user's chosen form over the shared-package alternative; the consequence is that effective-scope results must be cached at the consumer to keep the retrieval hot path HTTP-free (D5).

**D5 — Data-level enforcement is authz-owned Postgres Row-Level Security (RLS).** authz owns the RLS policy on `knowledge_evidence`: a `FOR SELECT` policy filters rows by the `app.visible_scope` session setting (`*` = project-wide, otherwise a comma-joined list of visible department ids; unset = nothing visible, fail-closed), while write policies stay permissive because endpoint-level writes are gated by `authorize`. RAG builds no predicate and does not know `access_scope` participates in access: it only sets the session setting from an opaque `scope_context` the caller obtained from authz's `visible-scope`. This keeps RAG a permission-free content engine and pushes filtering to the database.

**D6 — Scope canonicalization stays in authz; RAG is authz-free.** The `default` project alias is resolved by authz (`canonical_project_id`) inside `visible_scope`, and by RAG's own `resolve_project_id` for callers that bypass the gate. RAG's `/api/scope/resolve` remains as pure project-id canonicalization (no authorization), and RAG no longer imports or calls authz at all.

## Authz service interface (sketch)

```text
GET  /api/v1/authz/health
POST /api/v1/authz/authorize            # {principal, action, resource} -> {allowed, reason?}
POST /api/v1/authz/visible-scope        # {principal, kb_id, project_id} -> {allowed, project_id, scope_context}

# Admin CRUD (authz owns these tables)
GET/POST /api/v1/authz/users
GET/POST /api/v1/authz/roles
GET/POST /api/v1/authz/grants
GET/POST /api/v1/authz/departments      # parent_id supported
```

## Data model (sketch)

```text
authz 服务拥有:   users, roles, grants, departments
rag 服务拥有:     knowledge_bases, projects, knowledge_evidence
session 服务拥有: chat_messages, chat_session_tombstones

departments(id, kb_id, parent_id NULL, name, UNIQUE(kb_id, name))
users(id, external_sub UNIQUE, display_name)
roles(id, kb_id, name, description, UNIQUE(kb_id, name))
grants(principal_id, principal_type, role_id, project_id NULL, department_id NULL, UNIQUE(...))
```

A chunk's visibility is derived from `grants` joined through `departments` (tree descent), never from a caller-supplied string. `knowledge_evidence.access_scope` is the opaque access key authz's RLS policy reads; RAG stores it as a black box with no knowledge of departments.

## Considered Options

- **Shared `authz/` Python package imported by rag and session.** Rejected in favor of D4 by decision: a package is simpler and lets the predicate be built in-process, but the user chose a deployable service to match the rag/session split, keep authorization independently versioned and operated, and allow non-Python consumers later. The cost — effective-scope results must cross a process boundary — is paid once per TTL window, not per query (D5).
- **Gateway-only enforcement.** Rejected: an edge gateway can guard endpoints but cannot express row-level visibility; RLS, owned by authz, pushes that visibility into the database so RAG stays permission-free and `ask` (retrieve + generate) needs no splitting.
- **Keep the department label and add grants on top.** Rejected: a flat label cannot express organizational inheritance or multi-department visibility, both of which D3 requires.
- **`general` kept as a magic string.** Rejected: it bypassed every check; as a real department it obeys the same rules as everything else.

## Consequences

- **Trust boundary moves, it does not disappear.** The edge must strip any client-supplied `x-principal` and inject the authenticated one; until SSO exists, authz is the trust anchor for identity, and the edge is responsible for who may claim a principal.
- **The retrieval hot path stays local.** RAG makes no authz round trip: the scope arrives as the opaque `x-scope-context` header and is applied via `set_config` before each evidence query. The authz gate (or client) is responsible for calling `visible-scope` and forwarding the result.
- **All permission logic concentrates in authz.** Role resolution, department-tree inheritance, `default`/`general` canonicalization, and the RLS policy live in exactly one module. RAG's `ensure_kb`/`ensure_project`-style existence checks remain only as its own relevance-boundary validation. The deletion test passes: without authz, the authorization logic would reappear across every RAG, session, and web call site.
- **The interface is small and the module is deep.** Callers learn `authorize` and `visible_scope`; RAG learns only "pass `x-scope-context` through to the database". Behind them sit role resolution, department trees, and the RLS policy. Tests cross the same seam: authz unit tests cover tree inheritance and grant precedence; RAG tests assert its retrieval SQL contains no permission predicate and that the scope header is forwarded verbatim.
- **RAG is a permission-free engine.** It no longer imports or calls authz; `retrieve`/`ask`/`evidence` default `x-scope-context` to `*` (project-wide) and write endpoints are trusted-edge until the authz gate is wired in Phase 3.
- **Phase 2 leaves org-listing endpoints unfiltered.** `GET /api/knowledge-bases`, `/api/projects`, and `/api/documents` still return their full rows; they expose names and ids only, not content, and Phase 3 filters them by the principal's grants.

## Migration

- **Phase 0 — schema.** `departments` (tree), `users`, `roles`, `grants` in authz `init_db`, reusing the existing `init_db`/shared-DB convention, seeded with the three default roles and a bootstrap admin. The public department is the built-in `general` id, so existing `knowledge_evidence` rows need no migration.
- **Phase 1 — authz service.** Health, `authorize`, `visible_scope`, and admin CRUD, with unit tests for tree inheritance and grant precedence.
- **Phase 2 — RLS enforcement.** authz `init_db` enables RLS + policies on `knowledge_evidence` (read policy fail-closed on `app.visible_scope`, permissive write policies). RAG renames the legacy `department` column to the opaque `access_scope` key, drops every permission predicate from `search`/`get_evidence`, and applies the opaque `x-scope-context` header via `set_config`; RAG no longer imports authz.
- **Phase 3 — session and web.** Session stops calling back for permission purposes; the department dropdown and project list come from authz-derived endpoints filtered by the principal's grants; `ScopeBar` no longer lets the user pick an arbitrary department.
- **Phase 4 — optional SSO.** OIDC/LDAP plugs into the `x-principal` seam; authz synchronizes `users.external_sub`.

## Open questions

- **Bootstrap admin.** How is the first admin provisioned before any SSO exists (seed user, env var, or an authz CLI)?
- **Knowledge-base-level grants.** Should `grants` also support a kb-level scope (whole knowledge base) in addition to project/department?
- **Public-department placement.** Resolved: the public department keeps the well-known id `general` as a built-in constant (`authz.store.PUBLIC_DEPARTMENT`), not a `departments` row and not a nullable column on evidence. It is always valid for ingestion and always visible within an accessible Project, which preserves the legacy `general` behavior without a data migration.
