---
status: accepted
---

# Use a stable project-owned Chunk Tokenizer

The project uses `o200k_base` as its stable Chunk Tokenizer for measuring the Chunk Size Limit and Overlap Target. It does not describe `o200k_base` as Qwen3 Embedding's native tokenizer. Qwen3 Embedding uses a Qwen2 tokenizer with a different vocabulary and produces different token counts for some input text.

This separates two concerns: retrieval chunk granularity remains stable when the embedding model changes, while model-specific input limits remain the responsibility of the embedding request boundary.

## Considered Options

- Use the configured embedding model's tokenizer for chunking. Rejected as the project-wide default because changing embedding models would also change Chunk boundaries and could require re-chunking the corpus.
- Use `o200k_base` and claim that it exactly matches Qwen3. Rejected because official tokenizer files and target-service measurements show different vocabularies and token counts.
- Use `o200k_base` as a project-owned Chunk Tokenizer and validate model limits separately. Accepted because it provides stable chunking semantics without conflating them with model capacity.

## Consequences

The Chunk Size Limit is a hard invariant under the Chunk Tokenizer, not automatically under every Model Tokenizer. Code and documentation must name the tokenizer when ambiguity matters. Before sending text to an embedding model, the integration must enforce that model's actual input limit using its Model Tokenizer or an equivalent service-side validation. The supporting evidence is recorded in `docs/qwen3-tokenizer-research.md`.
