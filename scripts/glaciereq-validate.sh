#!/usr/bin/env bash
set -euo pipefail

cargo metadata --locked --no-deps --format-version 1 >/dev/null
cargo fmt --all -- --check
cargo check --locked -p xai-grok-pager-bin
cargo test --locked -p xai-grok-config
cargo clippy --locked -p xai-grok-pager-bin --all-targets -- -D warnings
