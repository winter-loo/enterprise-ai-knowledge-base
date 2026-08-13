from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

EMBEDDING_DIMENSIONS = 1024
QUERY_INSTRUCTION = "Given a user question about internal enterprise knowledge, retrieve relevant passages that answer the question"


def connect() -> psycopg.Connection:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(database_url, row_factory=dict_row)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with connect() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_bases (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id), name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL, UNIQUE(kb_id, name)
            );
            CREATE TABLE IF NOT EXISTS knowledge_evidence (
                id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id), project_id TEXT NOT NULL REFERENCES projects(id),
                document_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, content TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                embedding vector(1024) NOT NULL, department TEXT NOT NULL DEFAULT 'general', status TEXT NOT NULL DEFAULT 'READY',
                source_type TEXT NOT NULL DEFAULT 'upload', source_uri TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
                page INTEGER, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_scope ON knowledge_evidence(kb_id, project_id, department, status);
            CREATE INDEX IF NOT EXISTS idx_evidence_fts ON knowledge_evidence USING GIN (to_tsvector('simple', content));
            CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id);
        """)
        dimensions = conn.execute("""
            SELECT atttypmod FROM pg_attribute
            WHERE attrelid='knowledge_evidence'::regclass AND attname='embedding'
        """).fetchone()["atttypmod"]
        if dimensions != EMBEDDING_DIMENSIONS:
            count = conn.execute("SELECT count(*) AS count FROM knowledge_evidence").fetchone()["count"]
            if count:
                raise RuntimeError(f"knowledge_evidence contains {count} incompatible vectors; clear it and restart")
            conn.execute(f"ALTER TABLE knowledge_evidence ALTER COLUMN embedding TYPE vector({EMBEDDING_DIMENSIONS})")
        conn.execute("INSERT INTO knowledge_bases VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", ("company", "公司知识库", "默认企业政策、产品和技术文档", now()))
        conn.execute("INSERT INTO projects VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ("default", "company", "默认项目", "默认项目范围", now()))
        conn.commit()


def ensure_kb(kb_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM knowledge_bases WHERE id=%s", (kb_id,)).fetchone()


def ensure_project(kb_id: str, project_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE kb_id=%s AND id=%s", (kb_id, project_id)).fetchone()
        if not row and project_id == "default":
            row = conn.execute("SELECT * FROM projects WHERE kb_id=%s ORDER BY created_at LIMIT 1", (kb_id,)).fetchone()
        return row


def list_kbs() -> list[dict[str, Any]]:
    with connect() as conn:
        return conn.execute("SELECT * FROM knowledge_bases ORDER BY created_at").fetchall()


def create_kb(name: str, description: str) -> dict[str, Any]:
    kb_id, project_id = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute("INSERT INTO knowledge_bases VALUES(%s,%s,%s,%s)", (kb_id, name, description, now()))
        conn.execute("INSERT INTO projects VALUES(%s,%s,%s,%s,%s)", (project_id, kb_id, "默认项目", "默认项目范围", now()))
        conn.commit()
    return {"id": kb_id, "name": name, "description": description, "default_project_id": project_id}


def list_projects(kb_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return conn.execute("SELECT * FROM projects WHERE kb_id=%s ORDER BY created_at", (kb_id,)).fetchall()


def create_project(kb_id: str, name: str, description: str) -> dict[str, str]:
    project_id = uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute("INSERT INTO projects VALUES(%s,%s,%s,%s,%s)", (project_id, kb_id, name, description, now()))
        conn.commit()
    return {"id": project_id, "name": name}


def embed(texts: list[str]) -> list[list[float]]:
    base_url = os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    api_key = os.getenv("EMBEDDING_API_KEY", "ollama")
    model = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    with httpx.Client(timeout=120) as client:
        response = client.post(f"{base_url}/embeddings", headers={"Authorization": f"Bearer {api_key}"}, json={"model": model, "input": texts})
        response.raise_for_status()
        vectors = [item["embedding"] for item in response.json()["data"]]
    if len(vectors) != len(texts) or any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
        raise RuntimeError(f"embedding service must return one {EMBEDDING_DIMENSIONS}-dimension vector per input")
    return vectors


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def insert_document(*, kb_id: str, project_id: str, document_id: str, filename: str, department: str, parser: str, pdf_type: str | None, pages_needing_ocr: list[int], chunks: list[str], stored_path: str) -> dict[str, Any]:
    vectors = embed(chunks)
    created = now()
    with connect() as conn:
        for index, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):
            conn.execute("""
                INSERT INTO knowledge_evidence
                (id,kb_id,project_id,document_id,chunk_index,content,summary,embedding,department,status,source_type,source_uri,filename,page,metadata,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,'READY','upload',%s,%s,NULL,%s::jsonb,%s)
            """, (uuid.uuid4().hex, kb_id, project_id, document_id, index, content, content[:500], vector_literal(vector), department, stored_path, filename, json.dumps({"parser": parser, "pdf_type": pdf_type, "pages_needing_ocr": pages_needing_ocr}), created))
        conn.commit()
    return {"id": document_id, "filename": filename, "project_id": project_id, "status": "READY", "chunk_count": len(chunks), "parser": parser, "pdf_type": pdf_type, "pages_needing_ocr": pages_needing_ocr}


def list_documents(kb_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return conn.execute("""
            SELECT document_id AS id, max(filename) AS filename, max(project_id) AS project_id,
                   max(department) AS department, max(status) AS status, count(*) AS chunk_count,
                   max(source_type) AS source_type, max(metadata->>'parser') AS parser,
                   max(metadata->>'pdf_type') AS pdf_type, max(created_at) AS created_at
            FROM knowledge_evidence WHERE kb_id=%s GROUP BY document_id ORDER BY max(created_at) DESC
        """, (kb_id,)).fetchall()


def search(question: str, kb_id: str, project_id: str, department: str, top_k: int) -> list[dict[str, Any]]:
    instructed = f"Instruct: {QUERY_INSTRUCTION}\nQuery: {question}"
    query_vector = vector_literal(embed([instructed])[0])
    with connect() as conn:
        return conn.execute("""
            WITH candidates AS (
                SELECT e.*, 1 - (e.embedding <=> %s::vector) AS semantic_score,
                       ts_rank(to_tsvector('simple', e.content), websearch_to_tsquery('simple', %s)) AS lexical_score,
                       EXTRACT(EPOCH FROM (now() - e.created_at)) / 86400 AS age_days
                FROM knowledge_evidence e
                WHERE e.kb_id=%s AND e.project_id=%s
                  AND (e.department=%s OR e.department='general') AND e.status='READY'
            )
            SELECT candidates.*, (0.75 * semantic_score + 0.25 * lexical_score +
                0.1 / (1 + age_days / 30))::double precision AS score
            FROM candidates WHERE semantic_score > 0 OR lexical_score > 0
            ORDER BY score DESC LIMIT %s
        """, (query_vector, question, kb_id, project_id, department, top_k)).fetchall()


def add_message(session_id: str, role: str, content: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(%s,%s,%s,%s)", (session_id, role, content, now()))
        conn.commit()


def list_messages(session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if limit:
            return conn.execute(
                "SELECT role,content,created_at FROM (SELECT * FROM chat_messages WHERE session_id=%s ORDER BY id DESC LIMIT %s) m ORDER BY id",
                (session_id, limit),
            ).fetchall()
        return conn.execute("SELECT role,content,created_at FROM chat_messages WHERE session_id=%s ORDER BY id", (session_id,)).fetchall()


def clear_session(session_id: str) -> int:
    with connect() as conn:
        deleted = conn.execute("DELETE FROM chat_messages WHERE session_id=%s", (session_id,)).rowcount
        conn.commit()
        return deleted
