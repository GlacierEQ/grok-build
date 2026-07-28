#!/usr/bin/env python3
"""Validate the native GlacierEQ customization pack without third-party packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise AssertionError(message)


def validate_schemas() -> None:
    schema_dir = ROOT / "glaciereq" / "schemas"
    expected = {
        "action-receipt.schema.json",
        "memory-record.schema.json",
        "mission-contract.schema.json",
    }
    found = {path.name for path in schema_dir.glob("*.json")}
    if found != expected:
        fail(f"schema set mismatch: expected {sorted(expected)}, found {sorted(found)}")
    for path in schema_dir.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            fail(f"{path} does not declare JSON Schema 2020-12")
        if data.get("type") != "object" or not data.get("required"):
            fail(f"{path} lacks an object contract")


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        fail(f"{path} has no YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise AssertionError(f"{path} has unterminated frontmatter") from error
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if line.startswith(" ") or not line.strip() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def validate_skills_and_agents() -> None:
    expected_skills = {"echo-context", "repo-holograph", "continuity-handoff"}
    skills = {
        path.parent.name: path
        for path in (ROOT / ".grok" / "skills").glob("*/SKILL.md")
        if path.parent.name in expected_skills
    }
    if set(skills) != expected_skills:
        fail(f"skill set mismatch: {sorted(skills)}")
    for name, path in skills.items():
        frontmatter = parse_frontmatter(path)
        if frontmatter.get("name") != name or not frontmatter.get("description"):
            fail(f"{path} requires matching name and description")

    expected_agents = {
        "glaciereq-architect",
        "glaciereq-continuity",
        "glaciereq-verifier",
    }
    names = set()
    for path in (ROOT / ".grok" / "agents").glob("glaciereq-*.md"):
        frontmatter = parse_frontmatter(path)
        names.add(frontmatter.get("name", ""))
        if not frontmatter.get("description") or not frontmatter.get("tools"):
            fail(f"{path} requires description and tools")
    if names != expected_agents:
        fail(f"agent set mismatch: {sorted(names)}")


def receipt_hash(item: dict[str, object]) -> str:
    core = dict(item)
    core.pop("receipt_hash", None)
    encoded = json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_hook() -> None:
    hook_json = ROOT / ".grok" / "hooks" / "trust-spine.json"
    config = json.loads(hook_json.read_text(encoding="utf-8"))
    events = config.get("hooks", {})
    required = {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionDenied",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PostCompact",
        "SessionEnd",
    }
    if set(events) != required:
        fail("trust-spine event set is incomplete")

    script = ROOT / ".grok" / "hooks" / "trust-spine.py"
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        payloads = [
            {
                "hookEventName": "session_start",
                "sessionId": "test-session",
                "workspaceRoot": str(workspace),
                "timestamp": "2026-07-28T00:00:00Z",
                "prompt": "must be hashed, not stored",
            },
            {
                "hookEventName": "post_tool_use",
                "sessionId": "test-session",
                "workspaceRoot": str(workspace),
                "timestamp": "2026-07-28T00:00:01Z",
                "toolName": "search_replace",
                "toolInput": {"api_key": "must-not-leak", "file": "README.md"},
            },
        ]
        for payload in payloads:
            result = subprocess.run(
                [sys.executable, str(script)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                fail(f"hook execution failed: {result.stderr}")

        log_path = workspace / ".grok" / "runtime" / "trust-spine" / "receipts.jsonl"
        receipts = [json.loads(line) for line in log_path.read_text().splitlines()]
        if len(receipts) != 2:
            fail("hook did not emit two receipts")
        if receipts[0]["parent_hash"] is not None:
            fail("first receipt must start a chain")
        if receipts[1]["parent_hash"] != receipts[0]["receipt_hash"]:
            fail("second receipt does not link to first")
        for receipt in receipts:
            if receipt["receipt_hash"] != receipt_hash(receipt):
                fail("receipt hash verification failed")
        serialized = log_path.read_text(encoding="utf-8")
        if "must-not-leak" in serialized or "must be hashed" in serialized:
            fail("hook leaked raw sensitive content")


def main() -> int:
    validate_schemas()
    validate_skills_and_agents()
    validate_hook()
    print("GlacierEQ native pack validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
