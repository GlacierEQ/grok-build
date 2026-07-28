# GlacierEQ Grok Build Downstream

This repository is an actively customized downstream of `xai-org/grok-build`.
It is not a passive or read-only mirror.

## Branch model

| Branch | Purpose |
|---|---|
| `main` | Writable GlacierEQ product branch. All fine-tuning, integrations, fixes, and product work land here. |
| `upstream/main` | Exact machine-managed mirror of `xai-org/grok-build:main`. Never place GlacierEQ-only commits here. |
| `feature/*`, `fix/*`, `integration/*` | Short-lived work branches created from `main`. |
| `archive/*` | Immutable historical checkpoints before major syncs or migrations. |

The upstream mirror exists to preserve a clean comparison and update path. It does not restrict the writable product branch.

## Upstream update flow

1. The scheduled sync workflow fetches `xai-org/grok-build:main`.
2. It advances `upstream/main` to the exact upstream commit.
3. When `main` does not already contain that commit, it opens an upstream-sync pull request into `main`.
4. Resolve conflicts in favor of preserving both current upstream behavior and intentional GlacierEQ changes.
5. Run formatting, targeted compilation, tests, and Clippy before merging.

Never reset `main` to upstream after GlacierEQ customization begins. Upstream changes must be merged or rebased through a reviewable branch so downstream work remains visible and recoverable.

## Fine-tuning policy

GlacierEQ customization is expected and continuous. Appropriate work includes:

- MCP and connector integrations
- memory and retrieval architecture
- agent orchestration and subagent behavior
- local and cloud execution paths
- operator controls and automation
- Termux, macOS, Linux, Android-adjacent, and remote-node workflows
- telemetry, provenance, auditability, and recovery
- performance, reliability, and interface improvements

Generated files are not sacred. Prefer changing their source generator or per-crate manifest when one exists, then regenerate and commit the resulting output. Direct generated-file edits are acceptable only when the generation source is unavailable or the change is intentionally temporary and documented.

## Required validation

Use the pinned Rust toolchain and install DotSlash before builds requiring repository tools:

```bash
cargo install dotslash --locked
/usr/bin/env dotslash --help
cargo fmt --all -- --check
cargo check -p xai-grok-pager-bin
cargo test -p xai-grok-config
cargo clippy -p xai-grok-pager-bin --all-targets -- -D warnings
```

For narrow changes, validate the directly affected crate first, then run the composition-root check before merging.

## Conflict-resolution rule

Do not discard downstream behavior merely because upstream changed the same area. Classify each conflict as:

1. upstream replacement that fully supersedes the customization;
2. compatible changes that should be composed;
3. intentional downstream divergence that must be preserved and adapted;
4. obsolete customization that can be removed with an explicit commit explanation.

Every sync should leave an auditable explanation for non-trivial conflict decisions.
