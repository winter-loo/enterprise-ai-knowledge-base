---
status: accepted
supersedes: 0004-authz-service-with-resource-scoped-rbac
---

# Project grants with Principal-driven RLS

## Context

ADR 0004 made departments and an expiring `scope_context` capability part of
data visibility. The same short-lived string was later stored as the chat
session partition key. Reissuing a valid capability on refresh therefore made
the application treat an unchanged Project as a different authorization
context, hiding existing history and creating an empty session.

The company model has since been clarified: a Project is the smallest
knowledge authorization boundary. There is one company Knowledge Base, no
department-level access control, and no local employee directory yet.

## Decision

- The trusted gateway supplies one stable `x-principal` value. Development
  Vite injects `admin`; a future independent API Gateway validates Supabase
  JWTs and derives the Principal from issuer plus subject.
- Authz owns `project_grants(principal_id, project_id, role)` with exactly one
  fixed Viewer, Editor, or Manager role per pair. It also owns globally
  provisioned Platform Administrators.
- PostgreSQL RLS receives `app.principal_id` transaction-locally. Knowledge
  evidence requires a Project Grant or Platform Administrator; writes require
  Editor or above. Sessions require both their owner and a current Project
  Grant, with no Platform Administrator bypass.
- `scope_context`, signed scope capabilities, departments, custom roles,
  local users, browser session tokens, and their APIs are removed.
- A Session records its Project and owner server-side. The server lists only
  currently authorized, owner-visible history; clients cache UI state only.

## Consequences

Revoking a Project Grant takes effect on the next database query without a
TTL window. A refresh or normal identity-token rotation does not change chat
history identity. The system deliberately has no group model, audit log, or
distributed Grant cache in this phase.

Existing development data is intentionally incompatible. It is removed only
through the explicit, environment-guarded `make reset-dev-data` command;
normal service startup never deletes it.
