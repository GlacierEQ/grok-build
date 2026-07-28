#!/usr/bin/env bash
set -euo pipefail

python3 scripts/validate-glaciereq-pack.py
cargo metadata --locked --no-deps --format-version 1 >/dev/null
cargo fmt --all -- --check
cargo check --locked -p xai-grok-pager-bin
cargo test --locked -p xai-grok-config
cargo clippy --locked -p xai-grok-pager-bin --all-targets -- -D warnings
