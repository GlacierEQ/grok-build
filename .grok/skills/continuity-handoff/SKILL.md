---
name: continuity-handoff
description: Package durable continuation context for another Grok session, subagent, ChatGPT, Claude, Codex, Gemini, or human operator. Use before context loss, session switching, agent handoff, compaction, or /continuity-handoff.
when-to-use: handoff, continue later, switch agent, cross-platform continuity, context window pressure
allowed-tools: read_file, list_dir, grep, run_terminal_command, search_tool, use_tool
argument-hint: target agent or next objective
metadata:
  author: GlacierEQ
  short-description: Durable cross-agent handoff
---

# Continuity Handoff

Create a continuation package that lets the next operator resume without reconstructing the project from chat history.

## Required sections

1. **Objective** — the real end state, not merely the last requested action.
2. **Current state** — branch, commit, working tree, active PRs, generated artifacts, and runtime status.
3. **Completed work** — only verified accomplishments.
4. **Decisions** — choices made, alternatives rejected, and why.
5. **Evidence** — source paths, hashes, commits, receipts, logs, and connector IDs.
6. **Open work** — ordered executable steps.
7. **Blockers and uncertainty** — failures, missing access, contradictions, and unverified assumptions.
8. **Recovery commands** — exact commands or tool calls needed to continue.
9. **Do-not-repeat context** — user constraints and settled decisions the next agent must preserve.

## Rules

- Keep raw secrets out.
- Prefer pointers over bulk copied content.
- Include exact dates and identifiers where ambiguity matters.
- Do not mark work complete merely because files were drafted.
- Store the package in a durable project location when writes are available.
