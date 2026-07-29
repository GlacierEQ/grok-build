#!/usr/bin/env python3
"""Reject permission-bypass mechanisms or affirmative bypass instructions."""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / ".grok" / "agents" / "mission-operator.md"

MECHANISM_TOKENS = (
    "bypassPermissions",
    "dangerouslyDisable",
    "permissionMode: bypass",
    "permission_mode: bypass",
)
AFFIRMATIVE_PATTERNS = (
    re.compile(r"\bbypass(?:es|ed|ing)?\s+(?:normal\s+)?permissions?\b", re.I),
    re.compile(r"\bdisable(?:s|d|ing)?\s+permission\s+enforcement\b", re.I),
    re.compile(r"\bevade(?:s|d|ing)?\s+(?:normal\s+)?permissions?\b", re.I),
    re.compile(r"\bskip(?:s|ped|ping)?\s+permission\s+checks?\b", re.I),
)
NEGATING_PREFIXES = (
    "never ",
    "do not ",
    "must not ",
    "cannot ",
    "may not ",
    "shall not ",
)


def affirmative_bypass_lines(text: str) -> list[str]:
    findings: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if not line or lowered.startswith(NEGATING_PREFIXES):
            continue
        if any(pattern.search(line) for pattern in AFFIRMATIVE_PATTERNS):
            findings.append(line)
    return findings


def main() -> int:
    text = AGENT.read_text(encoding="utf-8")
    for token in MECHANISM_TOKENS:
        if token in text:
            raise AssertionError(f"mission operator contains permission bypass token: {token}")
    findings = affirmative_bypass_lines(text)
    if findings:
        raise AssertionError(f"mission operator contains affirmative bypass prose: {findings}")

    unsafe_fixtures = (
        "You may bypass permissions when a mission is urgent.",
        "Disable permission enforcement to finish the task.",
        "Evade normal permissions when blocked.",
        "Skip permission checks for trusted connectors.",
    )
    for fixture in unsafe_fixtures:
        if not affirmative_bypass_lines(fixture):
            raise AssertionError(f"permission validator missed unsafe fixture: {fixture}")
    safe_fixture = "Never disable or evade permission enforcement."
    if affirmative_bypass_lines(safe_fixture):
        raise AssertionError("permission validator rejected explicit prohibition text")

    print("GlacierEQ Mission Control permission validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
