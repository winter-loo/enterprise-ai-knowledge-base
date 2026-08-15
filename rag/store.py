from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TypedDict, cast

import httpx
import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.sql import SQL

from rag.openai_responses import build_response_input, build_response_request, response_answer_text

EMBEDDING_DIMENSIONS = 1024
# Qwen3 Embedding 属于支持 instruction-aware retrieval 的向量模型。相同的一段文字, 在不同任务下可能应该产生不同的向量表示。
# 这条指令告诉模型:
# - 输入是用户查询, 不是普通文档
# - 任务是检索
# - 检索对象是企业内部知识
# - 希望找到能够回答问题的相关段落
# 这样生成的查询向量通常更适合与知识库片段的向量做相似度计算
QUERY_INSTRUCTION = "Given a user question about internal enterprise knowledge, retrieve relevant passages that answer the question"
SUMMARY_INSTRUCTIONS = "请将企业知识库片段压缩为一条简洁、忠实的摘要。摘要必须保持原文语言，不要翻译：中文原文输出中文，英文原文输出英文，其他语言同理。保留关键事实、条件、数字和专有名词，不添加原文没有的信息。只输出摘要。"
logger = logging.getLogger(__name__)


class EmbeddingData(TypedDict):
    embedding: list[float]


class EmbeddingResponse(TypedDict):
    data: list[EmbeddingData]


def connect() -> psycopg.Connection[DictRow]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.Connection[DictRow].connect(database_url, row_factory=dict_row)


def now() -> str:
    return datetime.now(UTC).isoformat()


def init_db() -> None:
    with connect() as conn:
        _ = conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        _ = conn.execute("""
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
            CREATE INDEX IF NOT EXISTS idx_evidence_scope ON knowledge_evidence(kb_id, project_id, department, status);
            CREATE INDEX IF NOT EXISTS idx_evidence_fts ON knowledge_evidence USING GIN (to_tsvector('simple', content));
        """)
        dimension_row = conn.execute("""
            SELECT atttypmod FROM pg_attribute
            WHERE attrelid='knowledge_evidence'::regclass AND attname='embedding'
        """).fetchone()
        if dimension_row is None:
            raise RuntimeError("knowledge_evidence.embedding column was not created")
        dimensions = cast(int, dimension_row["atttypmod"])
        if dimensions != EMBEDDING_DIMENSIONS:
            count_row = conn.execute("SELECT count(*) AS count FROM knowledge_evidence").fetchone()
            if count_row is None:
                raise RuntimeError("could not count knowledge_evidence rows")
            count = cast(int, count_row["count"])
            if count:
                raise RuntimeError(f"knowledge_evidence contains {count} incompatible vectors; clear it and restart")
            _ = conn.execute(SQL("ALTER TABLE knowledge_evidence ALTER COLUMN embedding TYPE vector(1024)"))
        _ = conn.execute(
            "INSERT INTO knowledge_bases VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", ("company", "公司知识库", "默认企业政策、产品和技术文档", now())
        )
        _ = conn.execute("INSERT INTO projects VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ("default", "company", "默认项目", "默认项目范围", now()))
        conn.commit()


def ensure_kb(kb_id: str) -> DictRow | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM knowledge_bases WHERE id=%s", (kb_id,)).fetchone()


def ensure_project(kb_id: str, project_id: str) -> DictRow | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE kb_id=%s AND id=%s", (kb_id, project_id)).fetchone()
        if not row and project_id == "default":
            row = conn.execute("SELECT * FROM projects WHERE kb_id=%s ORDER BY created_at LIMIT 1", (kb_id,)).fetchone()
        return row


def list_kbs() -> list[DictRow]:
    with connect() as conn:
        return conn.execute("SELECT * FROM knowledge_bases ORDER BY created_at").fetchall()


def create_kb(name: str, description: str) -> dict[str, str]:
    kb_id, project_id = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
    with connect() as conn:
        _ = conn.execute("INSERT INTO knowledge_bases VALUES(%s,%s,%s,%s)", (kb_id, name, description, now()))
        _ = conn.execute("INSERT INTO projects VALUES(%s,%s,%s,%s,%s)", (project_id, kb_id, "默认项目", "默认项目范围", now()))
        conn.commit()
    return {"id": kb_id, "name": name, "description": description, "default_project_id": project_id}


def list_projects(kb_id: str) -> list[DictRow]:
    with connect() as conn:
        return conn.execute("SELECT * FROM projects WHERE kb_id=%s ORDER BY created_at", (kb_id,)).fetchall()


def create_project(kb_id: str, name: str, description: str) -> dict[str, str]:
    project_id = uuid.uuid4().hex[:12]
    with connect() as conn:
        _ = conn.execute("INSERT INTO projects VALUES(%s,%s,%s,%s,%s)", (project_id, kb_id, name, description, now()))
        conn.commit()
    return {"id": project_id, "name": name}


def embed(texts: list[str]) -> list[list[float]]:
    base_url = os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    api_key = os.getenv("EMBEDDING_API_KEY", "ollama")
    model = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    with httpx.Client(timeout=120) as client:
        response = client.post(f"{base_url}/embeddings", headers={"Authorization": f"Bearer {api_key}"}, json={"model": model, "input": texts})
        _ = response.raise_for_status()
        payload = cast(EmbeddingResponse, response.json())
        vectors = [item["embedding"] for item in payload["data"]]
    if len(vectors) != len(texts) or any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
        raise RuntimeError(f"embedding service must return one {EMBEDDING_DIMENSIONS}-dimension vector per input")
    return vectors


def summarize_chunks(chunks: list[str]) -> list[str]:
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        return ["" for _ in chunks]

    summaries: list[str] = []
    try:
        with httpx.Client(timeout=45) as client:
            for index, chunk in enumerate(chunks):
                try:
                    response = client.post(
                        f"{base_url}/responses",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=build_response_request(
                            model,
                            SUMMARY_INSTRUCTIONS,
                            build_response_input([], chunk, 0),
                            max_output_tokens=160,
                        ),
                    )
                    response.raise_for_status()
                    summaries.append(response_answer_text(cast(object, response.json())))
                except (httpx.HTTPError, TypeError, ValueError) as exc:
                    logger.warning("LLM summary failed for chunk %d: %s", index, type(exc).__name__)
                    summaries.append("")
    except Exception as exc:
        logger.warning("LLM summary client failed: %s", type(exc).__name__)
        summaries.extend("" for _ in chunks[len(summaries) :])
    return summaries


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def insert_document(
    *,
    kb_id: str,
    project_id: str,
    document_id: str,
    filename: str,
    department: str,
    parser: str,
    pdf_type: str | None,
    pages_needing_ocr: list[int],
    chunks: list[str],
    stored_path: str,
    chunking_strategy: str = "recursive",
) -> dict[str, object]:
    vectors = embed(chunks)
    summaries = summarize_chunks(chunks)
    created = now()
    with connect() as conn:
        for index, (content, vector, summary) in enumerate(zip(chunks, vectors, summaries, strict=True)):
            _ = conn.execute(
                """
                INSERT INTO knowledge_evidence
                (id,kb_id,project_id,document_id,chunk_index,content,summary,embedding,department,status,source_type,source_uri,filename,page,metadata,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,'READY','upload',%s,%s,NULL,%s::jsonb,%s)
            """,
                (
                    uuid.uuid4().hex,
                    kb_id,
                    project_id,
                    document_id,
                    index,
                    content,
                    summary,
                    vector_literal(vector),
                    department,
                    stored_path,
                    filename,
                    json.dumps({"parser": parser, "pdf_type": pdf_type, "pages_needing_ocr": pages_needing_ocr, "chunking_strategy": chunking_strategy}),
                    created,
                ),
            )
        conn.commit()
    return {
        "id": document_id,
        "filename": filename,
        "project_id": project_id,
        "status": "READY",
        "chunk_count": len(chunks),
        "chunking_strategy": chunking_strategy,
        "parser": parser,
        "pdf_type": pdf_type,
        "pages_needing_ocr": pages_needing_ocr,
    }


def list_documents(kb_id: str) -> list[DictRow]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT document_id AS id, max(filename) AS filename, max(project_id) AS project_id,
                   max(department) AS department, max(status) AS status, count(*) AS chunk_count,
                   max(source_type) AS source_type, max(metadata->>'parser') AS parser,
                   max(metadata->>'pdf_type') AS pdf_type, max(metadata->>'chunking_strategy') AS chunking_strategy,
                   max(created_at) AS created_at
            FROM knowledge_evidence WHERE kb_id=%s GROUP BY document_id ORDER BY max(created_at) DESC
        """,
            (kb_id,),
        ).fetchall()


def search(question: str, kb_id: str, project_id: str, department: str, top_k: int) -> list[DictRow]:
    instructed = f"Instruct: {QUERY_INSTRUCTION}\nQuery: {question}"
    query_vector = vector_literal(embed([instructed])[0])
    with connect() as conn:
        return conn.execute(
            """
            -- 候选集只包含当前知识库和 Project 中，当前部门可见（本部门或 general）
            -- 且已完成索引的片段。权限过滤发生在评分前，避免越权片段进入召回结果。
            WITH candidates AS (
                SELECT e.*,
                       -- pgvector 的 <=> 返回余弦距离：cosine_distance = 1 - cosine_similarity。
                       -- 因此 1 - distance 还原为 cosine similarity。越接近 1，向量语义越相似；
                       -- 0 表示正交，负数表示方向相反。query_vector 是问题经 Qwen 检索指令编码后的向量。
                       1 - (e.embedding <=> %s::vector) AS semantic_score,
                       -- 将片段和问题转换为 PostgreSQL simple 配置的 tsvector/tsquery，
                       -- ts_rank 根据命中词及其位置计算词法相关性；命中越充分，分数通常越高。
                       ts_rank(to_tsvector('simple', e.content), websearch_to_tsquery('simple', %s)) AS lexical_score,
                       -- 片段创建至今的天数，供下面的新鲜度衰减项使用。
                       EXTRACT(EPOCH FROM (now() - e.created_at)) / 86400 AS age_days
                FROM knowledge_evidence e
                WHERE e.kb_id=%s AND e.project_id=%s
                  AND (e.department=%s OR e.department='general') AND e.status='READY'
            )
            SELECT candidates.*,
                   -- 最终分数 = 语义相似度权重 0.75 + 词法相关性权重 0.25 + 新鲜度奖励。
                   -- 新鲜度项为 0.1/(1+age_days/30)：当天约 0.1，30 天约 0.05，
                   -- 90 天约 0.025，只用于同等相关内容的轻量排序，不应压过相关性。
                   -- 0.75/0.25 是当前 MVP 的经验权重，不代表经过离线评测调优；
                   -- semantic_score 与 ts_rank 也并非天然处于完全相同的标度。
                   (0.75 * semantic_score + 0.25 * lexical_score +
                    0.1 / (1 + age_days / 30))::double precision AS score
            -- 至少保留语义正相关或有词法命中的片段，排除两个信号都无效的结果。
            FROM candidates WHERE semantic_score > 0 OR lexical_score > 0
            -- 按混合分降序，只返回调用方要求的 top_k 个片段。
            ORDER BY score DESC LIMIT %s
        """,
            (query_vector, question, kb_id, project_id, department, top_k),
        ).fetchall()


def get_evidence(chunk_id: str) -> DictRow | None:
    with connect() as conn:
        return conn.execute(
            """
            SELECT id, filename, chunk_index, content, summary, department, project_id,
                   document_id, source_type, source_uri, page, metadata, created_at
            FROM knowledge_evidence WHERE id=%s
            """,
            (chunk_id,),
        ).fetchone()
