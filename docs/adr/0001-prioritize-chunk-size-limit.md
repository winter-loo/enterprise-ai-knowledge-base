---
status: accepted
---

# Prioritize the chunk size limit over the overlap target

Final Chunks must preserve Semantic Coverage and must not exceed the Chunk Size Limit. Lossless reconstruction is not required: purely presentational whitespace may be normalized, but meaningful content cannot be dropped, reordered, or joined without a suitable delimiter. The Overlap Target is best-effort and may increase or decrease when exact overlap would violate a hard invariant. This priority is necessary because token counts can change after strings are trimmed or concatenated, so subtracting the requested overlap from the content budget cannot prove that the final Chunk will fit.

## Considered Options

- Treat overlap as exact and permit oversized Chunks. Rejected because downstream model limits require a hard upper bound.
- Truncate meaningful content to preserve both size and overlap. Rejected because it violates Semantic Coverage.
- Keep size and semantic coverage hard while treating overlap as a target. Accepted.

## Consequences

Every final Chunk must be measured after all text transformations. Overlap is measured by encoding the complete repeated source text independently; callers must not depend on receiving an exact token count at every boundary. Tiny Tail rebalancing is confined to a continuous source span and must not cross a Structural Boundary.
