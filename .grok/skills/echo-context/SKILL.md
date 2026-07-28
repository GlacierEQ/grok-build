---
name: echo-context
description: Build compact, provenance-aware context and durable memory records. Use when gathering prior work, reducing repeated context, reconciling conflicting facts, preparing a session, or asked for /echo-context.
when-to-use: context recovery, memory consolidation, prior-session continuity, token reduction, contradiction reconciliation
allowed-tools: read_file, list_dir, grep, web_search, search_tool, use_tool
argument-hint: objective or subject
metadata:
  author: GlacierEQ
  short-description: Provenance-aware context fabric
---

# Echo Context Fabric

Construct the smallest context package that preserves the evidence needed to continue the work correctly.

## Procedure

1. State the current objective and the decision or artifact this context must support.
2. Search local project memory and repository sources before asking the user to repeat information.
3. Prefer exact source pointers, hashes, file paths, commit SHAs, docket identifiers, or connector record IDs over copied bulk text.
4. Classify every substantive statement as one of:
   - `verified_fact`
   - `user_recollection`
   - `allegation`
   - `model_inference`
   - `recommendation`
   - `procedural_state`
   - `open_question`
5. Preserve contradictions. Never silently replace one statement with another.
6. Deduplicate exact content by SHA-256 and semantic duplicates by retaining the strongest source plus aliases.
7. Keep secrets and raw credentials out of memory.
8. Emit records compatible with `glaciereq/schemas/memory-record.schema.json`.
9. Finish with:
   - objective;
   - decisive context;
   - source pointers;
   - unresolved contradictions;
   - next executable action.

## Context economy

Do not dump entire transcripts or repositories into the active context. Externalize large material and retain stable pointers plus the exact sections needed for the current task.
