---
name: mission-control
description: Create, execute, verify, block, resume, audit, and complete durable GlacierEQ missions. Use for multi-step implementation, repository work, cross-system operations, completion contracts, or when asked for /mission-control.
when-to-use: multi-step implementation, completion enforcement, artifact verification, cross-system execution, durable task state
allowed-tools: Read, Grep, Glob, Bash, Write, Edit, search_tool, use_tool
argument-hint: mission objective or mission id
metadata:
  owner: GlacierEQ
  contract: glaciereq/schemas/mission-contract.schema.json
  runtime: .grok/runtime/missions/
---

# Mission Control

Use the executable control plane rather than keeping a multi-step mission only in chat.

## Procedure

1. Define an objective, explicit completion criteria, and at least one required artifact or required verification gate.
2. Create the mission before consequential implementation begins:

   ```bash
   python3 scripts/glaciereq-mission.py init <mission-id> \
     --objective "<objective>" \
     --criterion "<observable completion condition>" \
     --artifact "<required repository-relative artifact>" \
     --verification "<single executable command without shell pipelines>"
   ```

3. Perform the actual reads, writes, connector calls, tests, and repairs. This workflow is not read-only. Use the approved connector registry in `glaciereq/mission-control/connectors.json`; unlisted servers are not inherited by default.
4. Record current artifact hashes:

   ```bash
   python3 scripts/glaciereq-mission.py record-artifacts <mission-id>
   ```

   Any artifact change invalidates prior verification evidence.
5. Execute declared verification checks:

   ```bash
   python3 scripts/glaciereq-mission.py verify <mission-id>
   ```

6. Repair failures and rerun verification. Do not relabel a failed check as skipped merely to reach completion.
7. When a mission enters `blocked`, stop mutation and run the explicit transition only after the blocker is cleared:

   ```bash
   python3 scripts/glaciereq-mission.py resume <mission-id>
   ```

   A blocked mission rejects artifact recording, question resolution, criterion satisfaction, verification, and completion until it is resumed.
8. Resolve every open question through a journaled transition rather than editing mission JSON:

   ```bash
   python3 scripts/glaciereq-mission.py resolve-question <mission-id> \
     --index <zero-based-index> \
     --answer "<answer or disposition>"
   ```

   The event stores hashes of the question and answer, not the raw answer.
9. Explicitly satisfy each completion criterion with an evidence reference:

   ```bash
   python3 scripts/glaciereq-mission.py satisfy-criterion <mission-id> \
     --index <zero-based-index> \
     --evidence "<artifact, receipt, check, audit, or source reference>"
   ```

   Criterion evidence is hash-recorded in the event journal. Prose criteria alone never produce completion.
10. Run a deep integrity audit for consequential missions:

   ```bash
   python3 scripts/glaciereq-mission.py audit <mission-id> --deep
   ```

11. Complete only through the gate:

   ```bash
   python3 scripts/glaciereq-mission.py complete <mission-id>
   ```

   Completion is refused when an artifact is unrecorded or drifted, required evidence is invalid, a required check has not passed, a criterion is unsatisfied, a dependency is incomplete, or an open question remains.

## Operating rules

- Keep credentials and secrets out of objectives, command arguments, evidence references, and mission state.
- Split pipelines and compound shell expressions into separate verification entries; checks execute without a shell.
- Verification output is bounded and hashed while the process runs; raw stdout and stderr are not persisted.
- Treat `declared`, `available`, and `verified` connector status as different facts.
- Use `block --reason` when a real dependency prevents progress, and `resume` when it is cleared.
- Blocked is a real stop state: do not mutate or verify the mission until the journaled `resume` transition succeeds.
- Never manually edit `mission.json` or `events.jsonl`; state changes outside the journal are rejected.
- Mission runtime state is local and ignored by Git; durable product changes, schemas, documentation, and source remain in the repository.
- Never claim complete when the mission gate returns a blocker or failed required check.
