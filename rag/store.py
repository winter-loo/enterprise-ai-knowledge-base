from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypedDict, cast

import httpx
import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.sql import SQL

from shared.openai_responses import build_response_input, build_response_request, response_answer_text

EMBEDDING_DIMENSIONS = 1024
# 行级可见性的会话设置名,与 authz 服务的 RLS 策略约定一致:RAG 把调用方传来的
# 不透明 scope_context 写入该设置,由 Postgres RLS 行级过滤 knowledge_evidence。
# RAG 不解释 scope_context 的含义('*' 表示全项目可见)。
SCOPE_SETTING = "app.visible_scope"
SCOPE_ALL = "*"
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


class IndexProgress(TypedDict):
    stage: str
    message: str
    completed: int
    total: int
    percent: int


ProgressCallback = Callable[[IndexProgress], None]


class EmbeddingUnavailableError(RuntimeError):
    """Embedding 服务在有限重试后仍无法完成请求。"""


def connect(scope_context: str = SCOPE_ALL) -> psycopg.Connection[DictRow]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    conn = psycopg.Connection[DictRow].connect(database_url, row_factory=dict_row)
    # 把 authz 计算好的不透明可见范围应用到当前事务;Postgres RLS 策略据此在
    # 行级过滤 knowledge_evidence。RAG 只做透传,不知道 scope_context 的含义。
    conn.execute(f"SELECT set_config('{SCOPE_SETTING}', %s, true)", (scope_context,))
    return conn


def now() -> str:
    return datetime.now(UTC).isoformat()


def init_db() -> None:
    with connect() as conn:
        legacy = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name='knowledge_evidence' AND column_name='department'").fetchone()
        if legacy is not None:
            _ = conn.execute("DROP TABLE knowledge_evidence CASCADE")
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
                embedding vector(1024) NOT NULL, access_scope TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'READY',
                source_type TEXT NOT NULL DEFAULT 'upload', source_uri TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
                page INTEGER, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_scope ON knowledge_evidence(kb_id, project_id, access_scope, status);
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


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def embed(texts: list[str], on_progress: Callable[[int, int], None] | None = None) -> list[list[float]]:
    base_url = os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    api_key = os.getenv("EMBEDDING_API_KEY", "ollama")
    model = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    batch_size = _positive_int_env("EMBEDDING_BATCH_SIZE", 16)
    max_attempts = _positive_int_env("EMBEDDING_MAX_ATTEMPTS", 3)
    vectors: list[list[float]] = []
    with httpx.Client(timeout=120) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            batch_vectors: list[list[float]] | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    response = client.post(
                        f"{base_url}/embeddings",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"model": model, "input": batch},
                    )
                    _ = response.raise_for_status()
                    payload = cast(EmbeddingResponse, response.json())
                    batch_vectors = [item["embedding"] for item in payload["data"]]
                    break
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        raise
                    if attempt == max_attempts:
                        raise EmbeddingUnavailableError("Embedding 服务暂时不可用，请稍后重试") from exc
                    logger.warning("Embedding batch failed with %s (attempt %d/%d)", type(exc).__name__, attempt, max_attempts)
                    time.sleep(2 ** (attempt - 1))
            if batch_vectors is None:
                raise EmbeddingUnavailableError("Embedding 服务暂时不可用，请稍后重试")
            if len(batch_vectors) != len(batch) or any(len(vector) != EMBEDDING_DIMENSIONS for vector in batch_vectors):
                raise RuntimeError(f"embedding service must return one {EMBEDDING_DIMENSIONS}-dimension vector per input")
            vectors.extend(batch_vectors)
            if on_progress is not None:
                on_progress(len(vectors), len(texts))
    if len(vectors) != len(texts) or any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
        raise RuntimeError(f"embedding service must return one {EMBEDDING_DIMENSIONS}-dimension vector per input")
    return vectors


def summarize_chunks(chunks: list[str], on_progress: Callable[[int, int], None] | None = None) -> list[str]:
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        if on_progress is not None:
            on_progress(len(chunks), len(chunks))
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
                if on_progress is not None:
                    on_progress(index + 1, len(chunks))
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
    access_scope: str,
    parser: str,
    pdf_type: str | None,
    pages_needing_ocr: list[int],
    chunks: list[str],
    stored_path: str,
    chunking_strategy: str = "recursive",
    on_progress: ProgressCallback | None = None,
) -> dict[str, object]:
    def report(stage: str, message: str, completed: int, total: int, percent: int) -> None:
        if on_progress is not None:
            on_progress({"stage": stage, "message": message, "completed": completed, "total": total, "percent": percent})

    def embedding_progress(completed: int, total: int) -> None:
        report("embedding", "生成向量", completed, total, 30 + round(40 * completed / total))

    vectors = embed(chunks, on_progress=embedding_progress) if on_progress is not None else embed(chunks)

    def summary_progress(completed: int, total: int) -> None:
        report("summarizing", "生成摘要", completed, total, 70 + round(15 * completed / total))

    summaries = summarize_chunks(chunks, on_progress=summary_progress) if on_progress is not None else summarize_chunks(chunks)
    created = now()
    with connect() as conn:
        for index, (content, vector, summary) in enumerate(zip(chunks, vectors, summaries, strict=True)):
            _ = conn.execute(
                """
                INSERT INTO knowledge_evidence
                (id,kb_id,project_id,document_id,chunk_index,content,summary,embedding,access_scope,status,source_type,source_uri,filename,page,metadata,created_at)
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
                    access_scope,
                    stored_path,
                    filename,
                    json.dumps({"parser": parser, "pdf_type": pdf_type, "pages_needing_ocr": pages_needing_ocr, "chunking_strategy": chunking_strategy}),
                    created,
                ),
            )
            report("storing", "写入索引", index + 1, len(chunks), 85 + round(14 * (index + 1) / len(chunks)))
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
                   max(access_scope) AS access_scope, max(status) AS status, count(*) AS chunk_count,
                   max(source_type) AS source_type, max(metadata->>'parser') AS parser,
                   max(metadata->>'pdf_type') AS pdf_type, max(metadata->>'chunking_strategy') AS chunking_strategy,
                   max(created_at) AS created_at
            FROM knowledge_evidence WHERE kb_id=%s GROUP BY document_id ORDER BY max(created_at) DESC
        """,
            (kb_id,),
        ).fetchall()


def search(question: str, kb_id: str, project_id: str, scope_context: str, top_k: int) -> list[DictRow]:
    instructed = f"Instruct: {QUERY_INSTRUCTION}\nQuery: {question}"
    query_vector = vector_literal(embed([instructed])[0])
    with connect(scope_context) as conn:
        return conn.execute(
            """
            -- 候选集只包含当前知识库和 Project 中已完成索引的片段。行级可见性
            -- 由 authz 拥有的 Postgres RLS 策略强制,本 SQL 不含任何权限谓词:
            -- connect() 已把不透明 scope_context 写入会话设置,数据库在行级过滤。
            WITH candidates AS (
                SELECT e.*,
                       -- pgvector 的 <=> 返回余弦距离:cosine_distance = 1 - cosine_similarity。
                       -- 因此 1 - distance 还原为 cosine similarity。越接近 1,向量语义越相似;
                       -- 0 表示正交,负数表示方向相反。query_vector 是问题经 Qwen 检索指令编码后的向量。
                       1 - (e.embedding <=> %s::vector) AS semantic_score,
                       -- 将片段和问题转换为 PostgreSQL simple 配置的 tsvector/tsquery,
                       -- ts_rank 根据命中词及其位置计算词法相关性;命中越充分,分数通常越高。
                       ts_rank(to_tsvector('simple', e.content), websearch_to_tsquery('simple', %s)) AS lexical_score,
                       -- 片段创建至今的天数,供下面的新鲜度衰减项使用。
                       EXTRACT(EPOCH FROM (now() - e.created_at)) / 86400 AS age_days
                FROM knowledge_evidence e
                WHERE e.kb_id=%s AND e.project_id=%s AND e.status='READY'
            )
            SELECT candidates.*,
                   -- 最终分数 = 语义相似度权重 0.75 + 词法相关性权重 0.25 + 新鲜度奖励。
                   -- 新鲜度项为 0.1/(1+age_days/30):当天约 0.1,30 天约 0.05,
                   -- 90 天约 0.025,只用于同等相关内容的轻量排序,不应压过相关性。
                   -- 0.75/0.25 是当前 MVP 的经验权重,不代表经过离线评测调优;
                   -- semantic_score 与 ts_rank 也并非天然处于完全相同的标度。
                   (0.75 * semantic_score + 0.25 * lexical_score +
                    0.1 / (1 + age_days / 30))::double precision AS score
            -- 至少保留语义正相关或有词法命中的片段,排除两个信号都无效的结果。
            FROM candidates WHERE semantic_score > 0 OR lexical_score > 0
            -- 按混合分降序,只返回调用方要求的 top_k 个片段。
            ORDER BY score DESC LIMIT %s
        """,
            (query_vector, question, kb_id, project_id, top_k),
        ).fetchall()


def get_evidence(chunk_id: str, kb_id: str, project_id: str, scope_context: str) -> DictRow | None:
    with connect(scope_context) as conn:
        return conn.execute(
            """
            SELECT id, filename, chunk_index, content, summary, access_scope, project_id,
                   document_id, source_type, source_uri, page, metadata, created_at
            FROM knowledge_evidence
            WHERE id=%s AND kb_id=%s AND project_id=%s
            """,
            (chunk_id, kb_id, project_id),
        ).fetchone()
