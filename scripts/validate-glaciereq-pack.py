#!/usr/bin/env python3
"""Validate the native GlacierEQ customization pack without third-party packages."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import runpy
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_SCHEMA_KEYS = {
    "$schema",
    "$id",
    "title",
    "description",
    "type",
    "additionalProperties",
    "required",
    "properties",
    "const",
    "pattern",
    "minLength",
    "format",
    "enum",
    "minimum",
    "maximum",
    "items",
    "uniqueItems",
    "minItems",
}
SCHEMA_CONTRACTS = {
    "action-receipt.schema.json": {
        "required": {
            "schema_version",
            "receipt_id",
            "event_name",
            "timestamp",
            "session_id",
            "workspace",
            "payload_hash",
            "parent_hash",
            "receipt_hash",
        },
        "properties": {
            "schema_version",
            "receipt_id",
            "event_name",
            "timestamp",
            "session_id",
            "workspace",
            "permission_mode",
            "tool_name",
            "status",
            "payload_hash",
            "parent_hash",
            "receipt_hash",
            "metadata",
        },
    },
    "memory-record.schema.json": {
        "required": {
            "schema_version",
            "record_id",
            "source",
            "scope",
            "statement_class",
            "content",
            "content_hash",
            "observed_at",
            "confidence",
            "sensitivity",
        },
        "properties": {
            "schema_version",
            "record_id",
            "source",
            "scope",
            "statement_class",
            "content",
            "content_hash",
            "observed_at",
            "effective_at",
            "confidence",
            "sensitivity",
            "supersedes",
            "contradicts",
            "tags",
        },
    },
    "mission-contract.schema.json": {
        "required": {
            "schema_version",
            "mission_id",
            "objective",
            "completion_contract",
            "status",
            "created_at",
            "artifacts",
            "verification",
        },
        "properties": {
            "schema_version",
            "mission_id",
            "objective",
            "completion_contract",
            "status",
            "created_at",
            "updated_at",
            "dependencies",
            "artifacts",
            "verification",
            "open_questions",
        },
    },
}


def fail(message: str) -> None:
    raise AssertionError(message)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_schema_support(schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        fail(f"{path}: schema node must be an object")
    unsupported = set(schema) - SUPPORTED_SCHEMA_KEYS
    if unsupported:
        fail(f"{path}: unsupported schema keywords: {sorted(unsupported)}")
    for name, child in schema.get("properties", {}).items():
        validate_schema_support(child, f"{path}.properties.{name}")
    items = schema.get("items")
    if isinstance(items, dict):
        validate_schema_support(items, f"{path}.items")
    extra = schema.get("additionalProperties")
    if isinstance(extra, dict):
        validate_schema_support(extra, f"{path}.additionalProperties")


def type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    fail(f"unsupported JSON Schema type: {expected}")
    return False


def validate_instance(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    if "const" in schema and value != schema["const"]:
        fail(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        fail(f"{path}: value {value!r} is outside enum")

    expected_types = schema.get("type")
    if expected_types is not None:
        choices = [expected_types] if isinstance(expected_types, str) else expected_types
        if not any(type_matches(value, choice) for choice in choices):
            fail(f"{path}: invalid type {type(value).__name__}; expected {choices}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            fail(f"{path}: missing required properties {missing}")
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                validate_instance(item, properties[name], f"{path}.{name}")
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                fail(f"{path}: unexpected property {name}")
            if isinstance(additional, dict):
                validate_instance(item, additional, f"{path}.{name}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            fail(f"{path}: too few items")
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                validate_instance(item, items_schema, f"{path}[{index}]")
        if schema.get("uniqueItems"):
            encoded = [canonical(item) for item in value]
            if len(set(encoded)) != len(encoded):
                fail(f"{path}: duplicate array items")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            fail(f"{path}: string is too short")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            fail(f"{path}: string does not match {pattern}")
        if schema.get("format") == "date-time":
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise AssertionError(f"{path}: invalid date-time") from error
            if parsed.tzinfo is None:
                fail(f"{path}: date-time must include a timezone")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            fail(f"{path}: value below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            fail(f"{path}: value above maximum")


def expect_invalid(value: Any, schema: dict[str, Any], label: str) -> None:
    try:
        validate_instance(value, schema)
    except AssertionError:
        return
    fail(f"{label}: invalid instance unexpectedly passed")


def validate_schemas() -> dict[str, dict[str, Any]]:
    schema_dir = ROOT / "glaciereq" / "schemas"
    found = {path.name for path in schema_dir.glob("*.json")}
    if found != set(SCHEMA_CONTRACTS):
        fail(f"schema set mismatch: expected {sorted(SCHEMA_CONTRACTS)}, found {sorted(found)}")

    schemas: dict[str, dict[str, Any]] = {}
    for name, contract in SCHEMA_CONTRACTS.items():
        path = schema_dir / name
        schema = json.loads(path.read_text(encoding="utf-8"))
        validate_schema_support(schema)
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            fail(f"{path} does not declare JSON Schema 2020-12")
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            fail(f"{path} must be a closed object contract")
        if set(schema.get("required", [])) != contract["required"]:
            fail(f"{path} required-field contract drifted")
        if set(schema.get("properties", {})) != contract["properties"]:
            fail(f"{path} property contract drifted")
        schemas[name] = schema

    action = schemas["action-receipt.schema.json"]["properties"]
    if action["schema_version"].get("const") != 1:
        fail("action receipt schema version must remain 1")
    for name in ("receipt_id", "payload_hash", "receipt_hash"):
        if not action[name].get("pattern"):
            fail(f"action receipt {name} must retain a pattern")

    memory = schemas["memory-record.schema.json"]["properties"]
    if set(memory["statement_class"].get("enum", [])) != {
        "verified_fact",
        "user_recollection",
        "allegation",
        "model_inference",
        "recommendation",
        "procedural_state",
        "open_question",
    }:
        fail("memory statement classes drifted")
    if memory["confidence"].get("minimum") != 0 or memory["confidence"].get("maximum") != 1:
        fail("memory confidence range drifted")

    mission = schemas["mission-contract.schema.json"]["properties"]
    if "complete" not in mission["status"].get("enum", []):
        fail("mission status contract lost complete")

    memory_sample = {
        "schema_version": 1,
        "record_id": "mem_test",
        "source": {"system": "git", "uri": "repo://test", "artifact_hash": None},
        "scope": "project",
        "statement_class": "verified_fact",
        "content": "Verified test record",
        "content_hash": "a" * 64,
        "observed_at": "2026-07-28T00:00:00Z",
        "effective_at": None,
        "confidence": 1.0,
        "sensitivity": "internal",
        "supersedes": [],
        "contradicts": [],
        "tags": ["test"],
    }
    validate_instance(memory_sample, schemas["memory-record.schema.json"])
    invalid_memory = dict(memory_sample)
    invalid_memory["confidence"] = 2
    expect_invalid(invalid_memory, schemas["memory-record.schema.json"], "memory confidence")

    mission_sample = {
        "schema_version": 1,
        "mission_id": "mission_test",
        "objective": "Verify native pack",
        "completion_contract": ["All checks pass"],
        "status": "verifying",
        "created_at": "2026-07-28T00:00:00Z",
        "updated_at": None,
        "dependencies": [],
        "artifacts": [{"path": "AGENTS.md", "required": True, "hash": None}],
        "verification": [
            {"command": "python3 scripts/validate-glaciereq-pack.py", "required": True, "status": "pending", "evidence": None}
        ],
        "open_questions": [],
    }
    validate_instance(mission_sample, schemas["mission-contract.schema.json"])
    invalid_mission = dict(mission_sample)
    invalid_mission["status"] = "pretend-complete"
    expect_invalid(invalid_mission, schemas["mission-contract.schema.json"], "mission status")
    return schemas


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
    return hashlib.sha256(canonical(core).encode("utf-8")).hexdigest()


def read_receipts(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_linear_chain(receipts: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    previous = None
    identifiers = set()
    for index, receipt in enumerate(receipts):
        validate_instance(receipt, schema, f"receipt[{index}]")
        if receipt["parent_hash"] != previous:
            fail(f"receipt[{index}] does not link to its predecessor")
        if receipt["receipt_hash"] != receipt_hash(receipt):
            fail(f"receipt[{index}] hash verification failed")
        if receipt["receipt_id"] in identifiers:
            fail(f"receipt[{index}] reuses a receipt id")
        identifiers.add(receipt["receipt_id"])
        previous = receipt["receipt_hash"]


def validate_hook(action_schema: dict[str, Any]) -> None:
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
    for event_name, groups in events.items():
        for group in groups:
            for handler in group.get("hooks", []):
                command = handler.get("command", "")
                if "$(" in command or "git rev-parse" in command:
                    fail(f"{event_name}: hook command contains shell substitution")
                if not command.startswith("python3 -c ") or "runpy.run_path" not in command:
                    fail(f"{event_name}: hook command is not the static Python bootstrap")

    module = runpy.run_path(str(ROOT / ".grok" / "hooks" / "trust-spine.py"))
    emit_receipt = module["emit_receipt"]
    chain_error = module["ChainIntegrityError"]

    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        first = {
            "hookEventName": "session_start",
            "sessionId": "test-session",
            "workspaceRoot": str(workspace),
            "timestamp": "2026-07-28T00:00:00Z",
            "prompt": "must be hashed, not stored",
        }
        repeated = {
            "hookEventName": "post_tool_use",
            "sessionId": "test-session",
            "workspaceRoot": str(workspace),
            "timestamp": "2026-07-28T00:00:01Z",
            "toolName": "search_replace",
            "toolInput": {"api_key": "must-not-leak", "file": "README.md"},
        }
        emit_receipt(first)
        emit_receipt(repeated)
        emit_receipt(repeated)
        log_path = workspace / ".grok" / "runtime" / "trust-spine" / "receipts.jsonl"
        receipts = read_receipts(log_path)
        if len(receipts) != 3:
            fail("hook did not emit three receipts")
        assert_linear_chain(receipts, action_schema)
        serialized = log_path.read_text(encoding="utf-8")
        if "must-not-leak" in serialized or "must be hashed" in serialized:
            fail("hook leaked raw sensitive content")

    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        event = {
            "hookEventName": "session_start",
            "sessionId": "corruption-test",
            "workspaceRoot": str(workspace),
            "timestamp": "2026-07-28T00:00:00Z",
        }
        emit_receipt(event)
        log_path = workspace / ".grok" / "runtime" / "trust-spine" / "receipts.jsonl"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write('{"broken":')
        damaged = log_path.read_bytes()
        try:
            emit_receipt(event)
        except chain_error:
            pass
        else:
            fail("hook appended past a corrupted receipt log")
        if log_path.read_bytes() != damaged:
            fail("hook modified a corrupted receipt log")

    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        event = {
            "hookEventName": "post_tool_use",
            "sessionId": "concurrency-test",
            "workspaceRoot": str(workspace),
            "timestamp": "2026-07-28T00:00:00Z",
            "toolName": "read_file",
        }
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: emit_receipt(event), range(16)))
        if len({item["receipt_id"] for item in results}) != 16:
            fail("concurrent hooks produced duplicate receipt ids")
        log_path = workspace / ".grok" / "runtime" / "trust-spine" / "receipts.jsonl"
        receipts = read_receipts(log_path)
        if len(receipts) != 16:
            fail("concurrent hooks lost receipts")
        assert_linear_chain(receipts, action_schema)
        if log_path.with_suffix(log_path.suffix + ".lock").exists():
            fail("receipt lock file was not cleaned up")


def main() -> int:
    schemas = validate_schemas()
    validate_skills_and_agents()
    validate_hook(schemas["action-receipt.schema.json"])
    print("GlacierEQ native pack validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
