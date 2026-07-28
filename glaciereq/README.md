# GlacierEQ Native Custom Pack

This directory defines the portable contracts shared by the first GlacierEQ customization layer for Grok Build.

## Components

- **Echo Context Fabric** — compact, provenance-aware memory and continuation records.
- **Trust Spine** — append-only, SHA-256 hash-chained action receipts emitted by project hooks.
- **Repo Holograph** — a machine-readable map connecting this repository to the wider GlacierEQ system.

## Native extension surfaces

The pack is activated through Grok Build's existing project mechanisms:

- `.grok/skills/` for repeatable procedures;
- `.grok/agents/` for specialist session and subagent roles;
- `.grok/hooks/` for passive lifecycle receipts;
- `AGENTS.md` for repository-wide operating instructions.

No Rust behavior is replaced in this first slice. Future Rust changes should expose narrow adapters only when native extension surfaces cannot provide the required behavior.

## Runtime data

Trust Spine writes receipts beneath `.grok/runtime/trust-spine/`. Runtime files are intentionally ignored by Git. They may contain hashes and operational metadata, but the hook deliberately avoids storing raw prompts, tool inputs, tool outputs, or secrets.

## Contracts

Schemas live in `glaciereq/schemas/`:

- `action-receipt.schema.json`
- `memory-record.schema.json`
- `mission-contract.schema.json`

Validate the pack with:

```bash
python3 scripts/validate-glaciereq-pack.py
```
