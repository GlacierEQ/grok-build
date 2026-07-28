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
            "metadata",
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
    checks = {
        "null": value is None,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
    }
    if expected not in checks:
        fail(f"unsupported JSON Schema type: {expected}")
    return checks[expected]


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
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            fail(f"{path}: missing required properties {missing}")
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                validate_instance(item, properties[name], f"{path}.{name}")
            else:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    fail(f"{path}: unexpected property {name}")
                if isinstance(additional, dict):
                    validate_instance(item, additional, f"{path}.{name}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            fail(f"{path}: too few items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                validate_instance(item, schema["items"], f"{path}[{index}]")
        if schema.get("uniqueItems"):
            encoded = [canonical(item) for item in value]
            if len(set(encoded)) != len(encoded):
                fail(f"{path}: duplicate array items")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            fail(f"{path}: string is too short")
        if schema.get("pattern") and re.search(schema["pattern"], value) is None:
            fail(f"{path}: string does not match {schema['pattern']}")
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
    for name in ("receipt_id", "payload_hash", "receipt_hash"):
        if not action[name].get("pattern"):
            fail(f"action receipt {name} must retain a pattern")
    metadata = action["metadata"]
    expected_metadata = {
        "tool_use_id",
        "input_truncated",
        "result_truncated",
        "subagent_id",
        "subagent_type",
        "source",
    }
    if set(metadata.get("required", [])) != expected_metadata:
        fail("action receipt metadata required fields drifted")
    if set(metadata.get("properties", {})) != expected_metadata:
        fail("action receipt metadata properties drifted")

    memory = schemas["memory-record.schema.json"]["properties"]
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
            {
                "command": "python3 scripts/validate-glaciereq-pack.py",
                "required": True,
                "status": "pending",
                "evidence": None,
            }
        ],
        "open_questions": [],
    }
    validate_instance(mission_sample, schemas["mission-contract.schema.json"])
    invalid_mission = dict(mission_sample)
    invalid_mission["status"] = "pretend-complete"
    expect_invalid(invalid_mission, schemas["mission-contract.schema.json"], "mission status")
    return schemas


def yaml_scalar(raw: str) -> Any:
    value = raw.strip()
    if value in {"null", "~"}:
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def indent_of(line: str) -> int:
    if "\t" in line[: len(line) - len(line.lstrip())]:
        fail("frontmatter indentation must use spaces")
    return len(line) - len(line.lstrip(" "))


def next_content_line(lines: list[str], index: int) -> int:
    while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("#")):
        index += 1
    return index


def parse_yaml_block(lines: list[str], start: int, indent: int) -> tuple[Any, int]:
    start = next_content_line(lines, start)
    if start >= len(lines):
        return {}, start
    is_list = lines[start].lstrip().startswith("- ")
    result: Any = [] if is_list else {}
    index = start

    while index < len(lines):
        index = next_content_line(lines, index)
        if index >= len(lines):
            break
        line = lines[index]
        current = indent_of(line)
        if current < indent:
            break
        if current != indent:
            fail(f"unsupported frontmatter indentation near: {line}")
        stripped = line.strip()

        if is_list:
            if not stripped.startswith("- "):
                fail("mixed mapping/list frontmatter block")
            item = stripped[2:].strip()
            if not item:
                child_index = next_content_line(lines, index + 1)
                if child_index >= len(lines) or indent_of(lines[child_index]) <= indent:
                    result.append(None)
                    index += 1
                else:
                    child, index = parse_yaml_block(lines, child_index, indent_of(lines[child_index]))
                    result.append(child)
            else:
                result.append(yaml_scalar(item))
                index += 1
            continue

        if stripped.startswith("- ") or ":" not in stripped:
            fail(f"invalid frontmatter mapping line: {line}")
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            fail("empty frontmatter key")
        if raw_value.strip():
            result[key] = yaml_scalar(raw_value)
            index += 1
            continue
        child_index = next_content_line(lines, index + 1)
        if child_index >= len(lines) or indent_of(lines[child_index]) <= indent:
            result[key] = None
            index += 1
        else:
            child, index = parse_yaml_block(lines, child_index, indent_of(lines[child_index]))
            result[key] = child
    return result, index


def parse_frontmatter(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        fail(f"{path} has no YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise AssertionError(f"{path} has unterminated frontmatter") from error
    parsed, consumed = parse_yaml_block(lines[1:end], 0, 0)
    if consumed != len(lines[1:end]):
        remaining = next_content_line(lines[1:end], consumed)
        if remaining != len(lines[1:end]):
            fail(f"{path} frontmatter was not fully parsed")
    if not isinstance(parsed, dict):
        fail(f"{path} frontmatter must be a mapping")
    return parsed


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
    echo_text = skills["echo-context"].read_text(encoding="utf-8")
    if "Sanitize every external search query" not in echo_text:
        fail("echo-context must require external-search sanitization")

    block_fixture = [
        "name: block-test",
        "tools:",
        "  - Read",
        "  - Grep",
        "metadata:",
        "  owner: GlacierEQ",
    ]
    block_parsed, _ = parse_yaml_block(block_fixture, 0, 0)
    if block_parsed.get("tools") != ["Read", "Grep"]:
        fail("block-style frontmatter list parsing failed")
    if block_parsed.get("metadata") != {"owner": "GlacierEQ"}:
        fail("block-style frontmatter mapping parsing failed")

    expected_agents = {
        "glaciereq-architect",
        "glaciereq-continuity",
        "glaciereq-verifier",
    }
    names = set()
    for path in (ROOT / ".grok" / "agents").glob("glaciereq-*.md"):
        frontmatter = parse_frontmatter(path)
        names.add(frontmatter.get("name", ""))
        tools = frontmatter.get("tools")
        if not frontmatter.get("description") or not tools:
            fail(f"{path} requires description and tools")
        text = path.read_text(encoding="utf-8")
        if "MCP inheritance is intentional" not in text:
            fail(f"{path} must document inherited-MCP discipline")
        if "bypassPermissions" in text:
            fail(f"{path} must not bypass permissions")
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


def validate_head_state(path: Path, expected_count: int) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    stored = state.pop("state_hash")
    actual = hashlib.sha256(canonical(state).encode("utf-8")).hexdigest()
    if stored != actual:
        fail("receipt head state hash mismatch")
    if state.get("receipt_count") != expected_count:
        fail("receipt head count mismatch")
    return state


def validate_hook(action_schema: dict[str, Any]) -> None:
    hook_json = ROOT / ".grok" / "hooks" / "trust-spine.json"
    events = json.loads(hook_json.read_text(encoding="utf-8")).get("hooks", {})
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
        events_under_test = [
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
                "toolUseId": "tool-1",
                "toolInput": {"api_key": "must-not-leak", "file": "README.md"},
                "toolResultTruncated": True,
            },
            {
                "hookEventName": "subagent_start",
                "sessionId": "test-session",
                "workspaceRoot": str(workspace),
                "timestamp": "2026-07-28T00:00:02Z",
                "subagentId": "child-17",
                "subagentType": "architect",
            },
        ]
        for event in events_under_test:
            emit_receipt(event)
        log_path = workspace / ".grok" / "runtime" / "trust-spine" / "receipts.jsonl"
        head_path = log_path.with_suffix(log_path.suffix + ".head.json")
        receipts = read_receipts(log_path)
        assert_linear_chain(receipts, action_schema)
        if receipts[1]["metadata"]["result_truncated"] is not True:
            fail("tool result truncation was not retained")
        if receipts[2]["metadata"]["subagent_id"] != "child-17":
            fail("subagent id was not retained")
        serialized = log_path.read_text(encoding="utf-8")
        if "must-not-leak" in serialized or "must be hashed" in serialized:
            fail("hook leaked raw sensitive content")
        validate_head_state(head_path, 3)

        original_verify = emit_receipt.__globals__["verify_chain"]
        emit_receipt.__globals__["verify_chain"] = lambda _handle: fail(
            "normal receipt append rescanned the complete log"
        )
        try:
            emit_receipt(events_under_test[1])
        finally:
            emit_receipt.__globals__["verify_chain"] = original_verify
        assert_linear_chain(read_receipts(log_path), action_schema)
        validate_head_state(head_path, 4)

        head_path.unlink()
        emit_receipt(events_under_test[1])
        assert_linear_chain(read_receipts(log_path), action_schema)
        validate_head_state(head_path, 5)

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
        validate_head_state(log_path.with_suffix(log_path.suffix + ".head.json"), 16)
        if log_path.with_suffix(log_path.suffix + ".lock").exists():
            fail("receipt lock file was not cleaned up")


def validate_relationship_manifest() -> None:
    text = (ROOT / "GLACIEREQ_RELATIONSHIPS.yaml").read_text(encoding="utf-8")
    if "generated_from_verified_repositories: true" in text:
        fail("relationship manifest must not assert unscoped repository verification")
    if text.count("provenance:") < 5:
        fail("every portfolio relationship requires provenance")
    if "live_integrations_verified: false" not in text:
        fail("relationship manifest must state live-integration verification status")


def main() -> int:
    schemas = validate_schemas()
    validate_skills_and_agents()
    validate_hook(schemas["action-receipt.schema.json"])
    validate_relationship_manifest()
    print("GlacierEQ native pack validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
