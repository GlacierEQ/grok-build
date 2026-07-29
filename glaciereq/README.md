# GlacierEQ Native Custom Pack

This directory defines the portable contracts shared by the GlacierEQ customization layer for Grok Build.

## Components

- **Echo Context Fabric** — compact, provenance-aware memory and continuation records.
- **Trust Spine** — append-only, SHA-256 hash-chained action receipts emitted by project hooks.
- **Repo Holograph** — a machine-readable map connecting this repository to the wider GlacierEQ system.
- **Mission Control Plane** — durable mission contracts, executable verification, artifact hashing, connector capability declarations, completion gates, and hash-linked mission events.

## Native extension surfaces

The pack is activated through Grok Build's existing project mechanisms:

- `.grok/skills/` for repeatable procedures;
- `.grok/agents/` for specialist session and subagent roles;
- `.grok/hooks/` for passive lifecycle receipts;
- `AGENTS.md` for repository-wide operating instructions.

No Rust behavior is replaced in the current slices. Future Rust changes should expose narrow adapters only when native extension surfaces cannot provide the required behavior.

## Runtime data

Trust Spine writes receipts beneath `.grok/runtime/trust-spine/`. Mission Control writes mission state, verification evidence metadata, transaction recovery files, and event journals beneath `.grok/runtime/missions/`. Runtime files are intentionally ignored by Git.

Trust Spine avoids storing raw prompts, tool inputs, tool outputs, or secrets. Mission verification stores command hashes, executable names, exit status, byte counts, and output hashes; raw stdout and stderr are not persisted.

## Contracts

Core schemas live in `glaciereq/schemas/`:

- `action-receipt.schema.json`
- `memory-record.schema.json`
- `mission-contract.schema.json`

Mission Control schemas and connector declarations live in `glaciereq/mission-control/`:

- `schemas/mission-event.schema.json`
- `schemas/connector-registry.schema.json`
- `connectors.json`

Connector entries are capability and trust declarations. A connector is not considered live or verified without explicit evidence in the registry.

## Operation

Create and execute a durable mission with:

```bash
python3 scripts/glaciereq-mission.py init example-mission \
  --objective "Produce and verify the requested artifact" \
  --criterion "Required artifact exists" \
  --artifact "path/to/artifact" \
  --verification "python3 scripts/validate-mission-control.py"
python3 scripts/glaciereq-mission.py record-artifacts example-mission
python3 scripts/glaciereq-mission.py verify example-mission
python3 scripts/glaciereq-mission.py complete example-mission
python3 scripts/glaciereq-mission.py audit example-mission --deep
```

Verification commands execute directly with `shell=False`; pipelines and compound shell expressions must be split into separate checks.

## Validation

Validate the native pack and Mission Control with:

```bash
python3 scripts/validate-glaciereq-pack.py
python3 scripts/validate-mission-control.py
```
