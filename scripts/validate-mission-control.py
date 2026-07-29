#!/usr/bin/env python3
"""Validate GlacierEQ Mission Control Plane v1 with only the standard library."""

from __future__ import annotations

import json
from pathlib import Path
import runpy
import shlex
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASE = runpy.run_path(str(ROOT / "scripts" / "validate-glaciereq-pack.py"))
fail = BASE["fail"]
parse_frontmatter = BASE["parse_frontmatter"]
validate_instance = BASE["validate_instance"]
validate_schema_support = BASE["validate_schema_support"]

SCHEMAS = {
    "connector-registry.schema.json": {
        "required": {"schema_version", "generated_at", "policy", "connectors"},
        "properties": {"schema_version", "generated_at", "policy", "connectors"},
    },
    "mission-event.schema.json": {
        "required": {
            "schema_version",
            "event_id",
            "mission_id",
            "event_type",
            "timestamp",
            "actor",
            "metadata",
            "details_hash",
            "parent_hash",
            "event_hash",
        },
        "properties": {
            "schema_version",
            "event_id",
            "mission_id",
            "event_type",
            "timestamp",
            "actor",
            "metadata",
            "details_hash",
            "parent_hash",
            "event_hash",
        },
    },
}


def load_schemas() -> dict[str, dict[str, Any]]:
    directory = ROOT / "glaciereq" / "mission-control" / "schemas"
    found = {path.name for path in directory.glob("*.json")}
    if found != set(SCHEMAS):
        fail(f"mission-control schema set mismatch: {sorted(found)}")
    loaded: dict[str, dict[str, Any]] = {}
    for name, contract in SCHEMAS.items():
        schema = json.loads((directory / name).read_text(encoding="utf-8"))
        validate_schema_support(schema)
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            fail(f"{name} does not declare JSON Schema 2020-12")
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            fail(f"{name} must be a closed object contract")
        if set(schema.get("required", [])) != contract["required"]:
            fail(f"{name} required-field contract drifted")
        if set(schema.get("properties", {})) != contract["properties"]:
            fail(f"{name} property contract drifted")
        loaded[name] = schema
    return loaded


def validate_registry(schema: dict[str, Any]) -> set[str]:
    path = ROOT / "glaciereq" / "mission-control" / "connectors.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    validate_instance(registry, schema)
    policy = registry["policy"]
    if not all(policy.values()):
        fail("connector registry policy flags must remain enabled")
    identifiers: set[str] = set()
    server_names: set[str] = set()
    for connector in registry["connectors"]:
        if connector["id"] in identifiers:
            fail(f"duplicate connector id: {connector['id']}")
        identifiers.add(connector["id"])
        overlap = server_names.intersection(connector["server_names"])
        if overlap:
            fail(f"connector server names are not unique: {sorted(overlap)}")
        server_names.update(connector["server_names"])
        if connector["live_status"] in {"available", "verified"} and not connector["evidence"]:
            fail(f"{connector['id']} claims live status without evidence")
        if connector["live_status"] == "declared" and connector["evidence"] is not None:
            fail(f"{connector['id']} declared status must not imply live evidence")
    required = {
        "github",
        "supermemory",
        "notion",
        "google_drive",
        "dropbox",
        "filesystem",
        "fileboss",
    }
    if server_names != required:
        fail(f"approved connector server set drifted: {sorted(server_names)}")
    return server_names


def validate_native_surfaces(server_names: set[str]) -> None:
    skill_path = ROOT / ".grok" / "skills" / "mission-control" / "SKILL.md"
    skill = parse_frontmatter(skill_path)
    if skill.get("name") != "mission-control" or not skill.get("description"):
        fail("mission-control skill frontmatter is incomplete")
    skill_text = skill_path.read_text(encoding="utf-8")
    for command in ("init", "record-artifacts", "verify", "complete", "audit"):
        if f"glaciereq-mission.py {command}" not in skill_text:
            fail(f"mission-control skill is missing {command} procedure")
    if "This workflow is not read-only" not in skill_text:
        fail("mission-control skill lost write-capable operating instruction")

    agent_path = ROOT / ".grok" / "agents" / "mission-operator.md"
    agent = parse_frontmatter(agent_path)
    if agent.get("name") != "mission-operator" or not agent.get("description"):
        fail("mission operator frontmatter is incomplete")
    tools = str(agent.get("tools", ""))
    if "Write" not in tools or "Edit" not in tools:
        fail("mission operator must remain write-capable")
    inheritance = agent.get("mcpInheritance")
    if not isinstance(inheritance, dict) or set(inheritance) != {"named"}:
        fail("mission operator requires named MCP inheritance")
    named = inheritance["named"]
    if not isinstance(named, list) or set(named) != server_names:
        fail("mission operator MCP allowlist must match the connector registry")
    text = agent_path.read_text(encoding="utf-8")
    if "bypass permissions" in text.lower():
        fail("mission operator must not bypass permissions")


def validate_event_journal(
    events: list[dict[str, Any]], schema: dict[str, Any], mission_id: str
) -> None:
    if not events:
        fail("mission event journal is empty")
    previous = None
    identifiers: set[str] = set()
    for index, event in enumerate(events):
        validate_instance(event, schema, f"event[{index}]")
        if event["mission_id"] != mission_id:
            fail("mission event references the wrong mission")
        if event["parent_hash"] != previous:
            fail("mission event chain is not linear")
        if event["event_id"] in identifiers:
            fail("mission event id was reused")
        identifiers.add(event["event_id"])
        previous = event["event_hash"]


def expect_mission_error(callable_value: Any, label: str) -> None:
    try:
        callable_value()
    except Exception as error:  # narrowed below without coupling to runpy class identity
        if error.__class__.__name__ != "MissionError":
            raise
        return
    fail(f"{label}: expected MissionError")


def validate_engine(event_schema: dict[str, Any]) -> None:
    module = runpy.run_path(str(ROOT / "scripts" / "glaciereq-mission.py"))
    create_mission = module["create_mission"]
    record_artifacts = module["record_artifacts"]
    verify_mission = module["verify_mission"]
    complete_mission = module["complete_mission"]
    audit_mission = module["audit_mission"]
    load_state = module["load_state"]
    read_events = module["read_events"]
    parse_command = module["parse_command"]
    mission_paths = module["mission_paths"]
    mission_schema = json.loads(
        (ROOT / "glaciereq" / "schemas" / "mission-contract.schema.json").read_text(
            encoding="utf-8"
        )
    )

    executable = shlex.quote(sys.executable)
    passing = f'{executable} -c "import sys; print(\'verified-output\'); sys.exit(0)"'
    failing = f'{executable} -c "import sys; sys.exit(7)"'

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        (root / "artifact.txt").write_text("mission artifact\n", encoding="utf-8")
        state = create_mission(
            root,
            "success-path",
            "Prove the mission state machine",
            ["Artifact exists", "Verification passes", "Audit passes"],
            artifacts=["artifact.txt"],
            verification=[passing],
            actor="validator",
        )
        validate_instance(state, mission_schema)
        state, missing = record_artifacts(root, "success-path", "validator")
        if missing or not state["artifacts"][0]["hash"]:
            fail("artifact recording did not produce a hash")
        state, failed = verify_mission(root, "success-path", "validator", timeout=30)
        if failed or state["verification"][0]["status"] != "passed":
            fail("passing verification did not pass")
        evidence_path = root / state["verification"][0]["evidence"]
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        if evidence.get("raw_output_stored") is not False:
            fail("verification evidence must not store raw output")
        if "verified-output" in evidence_path.read_text(encoding="utf-8"):
            fail("verification evidence leaked raw stdout")
        state, blockers = complete_mission(root, "success-path", "validator")
        if blockers or state["status"] != "complete":
            fail(f"complete mission was blocked: {blockers}")
        audit = audit_mission(root, "success-path", "validator", deep=True)
        if not audit.get("ok"):
            fail("deep mission audit did not pass")
        paths = mission_paths(root, "success-path")
        events = read_events(paths["events"])
        validate_event_journal(events, event_schema, "success-path")
        validate_instance(load_state(root, "success-path"), mission_schema)

        (root / "artifact.txt").write_text("artifact drift\n", encoding="utf-8")
        expect_mission_error(
            lambda: audit_mission(root, "success-path", "validator", deep=True),
            "artifact drift audit",
        )

        failed_state = create_mission(
            root,
            "failure-path",
            "Prove failed checks block completion",
            ["Required check passes"],
            verification=[failing],
            actor="validator",
        )
        validate_instance(failed_state, mission_schema)
        failed_state, failed_indexes = verify_mission(
            root, "failure-path", "validator", timeout=30
        )
        if failed_indexes != [0] or failed_state["status"] != "failed":
            fail("required verification failure was not retained")
        failed_state, blockers = complete_mission(root, "failure-path", "validator")
        if failed_state["status"] != "blocked" or not blockers:
            fail("failed mission completion was not blocked")

        expect_mission_error(
            lambda: parse_command("printf ok | cat"),
            "shell pipeline rejection",
        )

        corrupt = create_mission(
            root,
            "corrupt-path",
            "Prove journal corruption is rejected",
            ["Journal remains valid"],
            actor="validator",
        )
        validate_instance(corrupt, mission_schema)
        corrupt_paths = mission_paths(root, "corrupt-path")
        with corrupt_paths["events"].open("a", encoding="utf-8") as handle:
            handle.write('{"partial":')
        expect_mission_error(
            lambda: audit_mission(root, "corrupt-path", "validator"),
            "partial journal rejection",
        )

    source = (ROOT / "scripts" / "glaciereq-mission.py").read_text(encoding="utf-8")
    if "shell=True" in source or "os.system(" in source:
        fail("mission verification must not execute through a shell")
    if "shell=False" not in source:
        fail("mission verification must explicitly disable shell execution")


def main() -> int:
    schemas = load_schemas()
    server_names = validate_registry(schemas["connector-registry.schema.json"])
    validate_native_surfaces(server_names)
    validate_engine(schemas["mission-event.schema.json"])
    print("GlacierEQ Mission Control validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
