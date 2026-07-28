# GlacierEQ Grok Build Instructions

This repository is a writable GlacierEQ downstream of `xai-org/grok-build`.

## Operating contract

- Preserve upstream behavior unless a downstream requirement intentionally changes it.
- Prefer native Grok extension surfaces—skills, agents, personas, hooks, workflows, MCP, ACP, and project configuration—before adding invasive Rust patches.
- Do not stop at plans or placeholders when implementation and verification are possible.
- Never claim a build, test, deployment, connector, or external action succeeded without its actual result.
- Keep facts, user recollections, allegations, inferences, and recommendations distinguishable.
- Preserve provenance for consequential inputs and emit verifiable receipts for consequential actions.
- Keep secrets out of source, logs, memory records, fixtures, and generated artifacts.
- Fine-tuning is continuous: `main` is the writable product branch; `upstream/main` is the exact upstream mirror.

## Native GlacierEQ pack

Use these project skills when applicable:

- `/echo-context` — construct compact, source-aware context and memory records.
- `/repo-holograph` — map a repository into the GlacierEQ portfolio and maintain its relationship manifest.
- `/continuity-handoff` — package durable continuation context for another session, agent, or platform.

Project agents under `.grok/agents/` provide architecture, verification, and continuity roles. Passive hooks under `.grok/hooks/` write privacy-preserving, hash-chained receipts to ignored runtime storage.

## Completion standard

A task is complete only when the requested artifact or repository change exists, relevant validation has run, failures are reported honestly, and the next operator can recover the reasoning from durable files rather than chat history alone.
