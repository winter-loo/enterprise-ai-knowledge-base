# Enterprise AI Knowledge Base

This context defines the language of the knowledge base: how source documents are divided into retrieval units, and how retrieval, grounded answers, and conversation state are separated.

## Language

### Knowledge Chunking

**Chunk**:
A retrieval unit produced from a contiguous portion of a source document.
_Avoid_: Slice, segment

**Chunk Size Limit**:
The maximum permitted Chunk Tokenizer count of a final Chunk. This is a hard invariant for retrieval chunking, but is not a claim about a Model Tokenizer's count.
_Avoid_: Chunk size, preferred size

**Chunk Tokenizer**:
The project-owned tokenizer used to measure Chunk Size Limit and Overlap Target consistently. It is currently `o200k_base` and is intentionally independent of the configured embedding model.
_Avoid_: Qwen tokenizer, model tokenizer

**Model Tokenizer**:
The tokenizer native to the configured embedding model, used when validating that an embedding request fits the model's actual input limit. Qwen3 Embedding uses its Qwen2 tokenizer vocabulary, not `o200k_base`.
_Avoid_: Chunk tokenizer

**Overlap Target**:
The desired amount of complete source text repeated between adjacent Chunks, measured by encoding that repeated text independently. Its token count should be as close as practical to the requested value and may vary to preserve harder invariants.
_Avoid_: Overlap guarantee, exact overlap

**Tiny Tail**:
A final Chunk created by window arithmetic within one continuous source span whose newly covered content falls below the accepted usefulness threshold, excluding repeated overlap. A short but complete structural unit is not a Tiny Tail.
_Avoid_: Short Chunk

**Structural Boundary**:
A meaningful division created by document organization or a semantic topic change, such as a Markdown section boundary. Content on opposite sides belongs to different structural units.
_Avoid_: Split point, separator

**Semantic Coverage**:
The requirement that every meaningful part of a source document appears in at least one Chunk without reordering content or joining previously separated content without a suitable delimiter. Exact reconstruction and preservation of purely presentational whitespace are not required.
_Avoid_: Source Coverage, lossless reconstruction, completeness

### Retrieval & Conversation

**Scope**:
The triple (kb_id, project_id, department) that bounds which knowledge evidence a query may retrieve. Project controls the relevance boundary; department controls the access boundary; the two are not interchangeable.
_Avoid_: 权限范围, context, boundary

**Retrieve**:
A stateless primitive that turns a query into ranked Chunks from the knowledge base. It performs no generation and persists nothing.
_Avoid_: search, query, lookup

**Ask**:
A stateless primitive that turns a query plus inline History into a grounded answer with Citations. It can be streamed or returned whole, and it persists no conversation state.
_Avoid_: chat, generate, answer as a verb

**Citation**:
A reference attached to a grounded answer identifying the Chunk it was drawn from, such as its id, filename, chunk index, score, and excerpt.
_Avoid_: source, reference, footnote

**Session**:
The identity and message History of one conversation, owned by the session service. The RAG service has no knowledge of Sessions.
_Avoid_: chat, conversation, thread

**History**:
The list of prior role/content messages a caller passes inline to Ask. The RAG service reads it but never stores it.
_Avoid_: context, memory, transcript
