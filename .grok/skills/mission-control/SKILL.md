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

1. Define an objective and explicit completion criteria.
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

5. Execute declared verification checks:

   ```bash
   python3 scripts/glaciereq-mission.py verify <mission-id>
   ```

6. Repair failures and rerun verification. Do not relabel a failed check as skipped merely to reach completion.
7. Complete only through the gate:

   ```bash
   python3 scripts/glaciereq-mission.py complete <mission-id>
   ```

   Completion is refused when a required artifact is missing, a required check has not passed, a dependency is incomplete, or an open question remains.
8. Run a deep integrity audit for consequential missions:

   ```bash
   python3 scripts/glaciereq-mission.py audit <mission-id> --deep
   ```

## Operating rules

- Keep credentials and secrets out of objectives, command arguments, evidence, and mission state.
- Split pipelines and compound shell expressions into separate verification entries; checks execute without a shell.
- Treat `declared`, `available`, and `verified` connector status as different facts.
- Use `block --reason` when a real dependency prevents progress, and `resume` when it is cleared.
- Mission runtime state is local and ignored by Git; durable product changes, schemas, documentation, and source remain in the repository.
- Never claim complete when the mission gate returns a blocker or failed required check.
