---
name: repo-holograph
description: Map a repository into the GlacierEQ portfolio and maintain truthful README and relationship metadata. Use for repository audits, integration planning, README improvement, capability discovery, or /repo-holograph.
when-to-use: repo map, README audit, system architecture, capability graph, integration map
allowed-tools: read_file, list_dir, grep, run_terminal_command, search_tool, use_tool
argument-hint: repository path or mapping goal
metadata:
  author: GlacierEQ
  short-description: Repository star map
---

# Repo Holograph

Treat each repository as both a working component and an entry point into the wider GlacierEQ system.

## Procedure

1. Identify the repository's actual purpose from code, tests, manifests, and operational evidence.
2. Separate implemented, configured, tested, deployed, planned, and aspirational capabilities.
3. Map:
   - inputs and outputs;
   - public interfaces;
   - tools, MCP servers, hooks, skills, and agents;
   - schemas and artifacts;
   - upstream dependencies;
   - downstream consumers;
   - sibling repositories;
   - verification commands;
   - current limitations.
4. Update or create `GLACIEREQ_RELATIONSHIPS.yaml` using explicit `integration_status` values.
5. Improve the README only with claims supported by repository evidence.
6. Link related repositories by functional relationship, not by keyword proximity.
7. Identify one highest-leverage missing integration and one concrete verification slice.
8. Never claim a connector, deployment, or integration is live without a dated successful receipt or equivalent evidence.

## Output

Produce a compact architecture map, relationship changes, README changes, unresolved evidence gaps, and the next integration step.
