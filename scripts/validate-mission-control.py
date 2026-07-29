#!/usr/bin/env python3
"""Adversarially validate GlacierEQ Mission Control Plane v1."""

from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import shlex
import sys
import tempfile
import time
from typing import Any, Callable

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
    event_types = set(
        loaded["mission-event.schema.json"]["properties"]["event_type"]["enum"]
    )
    for required in {"criterion_satisfied", "question_resolved"}:
        if required not in event_types:
            fail(f"mission event contract lost {required}")
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
    for command in (
        "init",
        "record-artifacts",
        "verify",
        "resolve-question",
        "satisfy-criterion",
        "complete",
        "audit",
    ):
        if f"glaciereq-mission.py {command}" not in skill_text:
            fail(f"mission-control skill is missing {command} procedure")
    for phrase in (
        "This workflow is not read-only",
        "Prose criteria alone never produce completion",
        "Never manually edit `mission.json`",
    ):
        if phrase not in skill_text:
            fail(f"mission-control skill lost policy: {phrase}")

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
    if "bypassPermissions" in text or "dangerouslyDisable" in text:
        fail("mission operator must not disable permissions")


def expect_mission_error(callable_value: Callable[[], Any], label: str) -> str:
    try:
        callable_value()
    except Exception as error:  # runpy creates a distinct MissionError class identity
        if error.__class__.__name__ != "MissionError":
            raise
        return str(error)
    fail(f"{label}: expected MissionError")
    return ""


def evidence_file(root: Path, reference: str) -> Path:
    path_text, separator, digest = reference.rpartition("#")
    if not separator or len(digest) != 64:
        fail("verification evidence reference is malformed")
    return root / path_text


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


def validate_engine(event_schema: dict[str, Any]) -> None:
    module = runpy.run_path(str(ROOT / "scripts" / "glaciereq-mission.py"))
    create_mission = module["create_mission"]
    record_artifacts = module["record_artifacts"]
    verify_mission = module["verify_mission"]
    satisfy_criterion = module["satisfy_criterion"]
    resolve_question = module["resolve_question"]
    complete_mission = module["complete_mission"]
    audit_mission = module["audit_mission"]
    load_state = module["load_state"]
    normalize_state = module["normalize_state"]
    read_events = module["read_events"]
    compute_event_hash = module["compute_event_hash"]
    parse_command = module["parse_command"]
    mission_paths = module["mission_paths"]
    exclusive_lock = module["exclusive_lock"]
    load_locked_state = module["load_locked_state"]
    commit_locked = module["commit_locked"]
    main_cli = module["main"]
    mission_schema = json.loads(
        (ROOT / "glaciereq" / "schemas" / "mission-contract.schema.json").read_text(
            encoding="utf-8"
        )
    )

    executable = shlex.quote(sys.executable)
    passing = f'{executable} -c "import sys; print(\'verified-output\'); sys.exit(0)"'
    failing = f'{executable} -c "import sys; sys.exit(7)"'
    noisy = (
        f'{executable} -c "import os,time; '
        "[(os.write(1,b'x'*1024),time.sleep(0.001)) for _ in range(10000)]\""
    )

    minimal = normalize_state(
        {
            "schema_version": 1,
            "mission_id": "schema-minimal",
            "objective": "Accept the published optional shape",
            "completion_contract": ["A criterion"],
            "status": "active",
            "created_at": "2026-07-29T00:00:00Z",
            "artifacts": [{"path": "artifact.txt", "required": True}],
            "verification": [{"command": passing, "required": True}],
        }
    )
    if minimal["updated_at"] is not None or minimal["dependencies"] or minimal["open_questions"]:
        fail("runtime did not normalize optional mission fields")
    if minimal["artifacts"][0]["hash"] is not None:
        fail("runtime did not normalize optional artifact hash")
    if minimal["verification"][0]["status"] != "pending":
        fail("runtime did not normalize optional verification status")

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        artifact = root / "artifact.bin"
        artifact.write_bytes(b"mission-artifact\n" * 200000)

        state = create_mission(
            root,
            "success-path",
            "Prove the hardened mission state machine",
            ["Artifact exists", "Verification passes", "Audit passes"],
            artifacts=["artifact.bin"],
            verification=[passing],
            questions=["Which audit level is required?"],
            actor="validator",
        )
        validate_instance(state, mission_schema)
        state, missing = record_artifacts(root, "success-path", "validator")
        if missing or not state["artifacts"][0]["hash"]:
            fail("streaming artifact recording did not produce a hash")
        state, failed = verify_mission(root, "success-path", "validator", timeout=30)
        if failed or state["verification"][0]["status"] != "passed":
            fail("passing verification did not pass")
        reference = state["verification"][0]["evidence"]
        if not reference:
            fail("passing verification has no evidence reference")
        evidence_path = evidence_file(root, reference)
        evidence_original = evidence_path.read_bytes()
        evidence = json.loads(evidence_original)
        if evidence.get("raw_output_stored") is not False:
            fail("verification evidence must not store raw output")
        if "verified-output" in evidence_path.read_text(encoding="utf-8"):
            fail("verification evidence leaked raw stdout")

        blocked_state, blockers = complete_mission(root, "success-path", "validator")
        if blocked_state["status"] != "blocked" or not any(
            "criterion" in item for item in blockers
        ):
            fail("unsatisfied completion criteria did not block completion")
        if not any("open question" in item for item in blockers):
            fail("open question did not block completion")

        state = resolve_question(
            root, "success-path", 0, "Deep integrity audit", "validator"
        )
        if state["open_questions"]:
            fail("question resolution did not update mission state")
        audit = audit_mission(root, "success-path", "validator", deep=True)
        if not audit.get("ok") or not audit.get("audit_event_hash"):
            fail("deep mission audit did not produce evidence")
        criterion_evidence = [
            f"artifact:{state['artifacts'][0]['hash']}",
            reference,
            f"audit:{audit['audit_event_hash']}",
        ]
        for index, evidence_reference in enumerate(criterion_evidence):
            satisfy_criterion(
                root,
                "success-path",
                index,
                evidence_reference,
                "validator",
            )
        state, blockers = complete_mission(root, "success-path", "validator")
        if blockers or state["status"] != "complete":
            fail(f"fully satisfied mission was blocked: {blockers}")
        expect_mission_error(
            lambda: complete_mission(root, "success-path", "validator"),
            "terminal completion",
        )
        paths = mission_paths(root, "success-path")
        events = read_events(paths["events"], "success-path")
        validate_event_journal(events, event_schema, "success-path")
        validate_instance(load_state(root, "success-path"), mission_schema)

        evidence_path.write_text('{"tampered":true}\n', encoding="utf-8")
        expect_mission_error(
            lambda: audit_mission(root, "success-path", "validator"),
            "evidence tamper audit",
        )
        evidence_path.write_bytes(evidence_original)
        artifact.write_bytes(b"artifact drift\n")
        expect_mission_error(
            lambda: audit_mission(root, "success-path", "validator", deep=True),
            "artifact drift audit",
        )
        terminal_state = load_state(root, "success-path")
        if terminal_state["status"] != "complete":
            fail("terminal mission was downgraded after drift")

        drift_artifact = root / "drift.txt"
        drift_artifact.write_text("version one\n", encoding="utf-8")
        create_mission(
            root,
            "reverify-path",
            "Invalidate checks when artifacts change",
            ["Changed artifacts are reverified"],
            artifacts=["drift.txt"],
            verification=[passing],
            actor="validator",
        )
        record_artifacts(root, "reverify-path", "validator")
        verified, failed = verify_mission(root, "reverify-path", "validator", timeout=30)
        if failed or verified["verification"][0]["status"] != "passed":
            fail("reverification fixture did not initially pass")
        drift_artifact.write_text("version two\n", encoding="utf-8")
        refreshed, _ = record_artifacts(root, "reverify-path", "validator")
        if refreshed["verification"][0]["status"] != "pending":
            fail("artifact change did not invalidate prior verification")
        if refreshed["verification"][0]["evidence"] is not None:
            fail("artifact change retained stale verification evidence")

        create_mission(
            root,
            "failure-path",
            "Prove failed checks remain terminally recorded",
            ["Required check passes"],
            verification=[failing],
            actor="validator",
        )
        failed_state, failed_indexes = verify_mission(
            root, "failure-path", "validator", timeout=30
        )
        if failed_indexes != [0] or failed_state["status"] != "failed":
            fail("required verification failure was not retained")

        create_mission(
            root,
            "launch-error",
            "Prove launch failures are recorded",
            ["Executable launches"],
            verification=["definitely-missing-glaciereq-executable"],
            actor="validator",
        )
        launch_state, launch_failed = verify_mission(
            root, "launch-error", "validator", timeout=30
        )
        if launch_failed != [0] or launch_state["status"] != "failed":
            fail("missing executable did not produce a failed mission")
        launch_evidence = json.loads(
            evidence_file(root, launch_state["verification"][0]["evidence"]).read_text(
                encoding="utf-8"
            )
        )
        if launch_evidence["launch_error_type"] != "FileNotFoundError":
            fail("missing executable evidence lost its launch error")

        engine_globals = verify_mission.__globals__
        original_limit = engine_globals["MAX_EVIDENCE_BYTES"]
        engine_globals["MAX_EVIDENCE_BYTES"] = 4096
        try:
            create_mission(
                root,
                "output-limit",
                "Prove output floods terminate safely",
                ["Output remains bounded"],
                verification=[noisy],
                actor="validator",
            )
            output_state, output_failed = verify_mission(
                root, "output-limit", "validator", timeout=30
            )
        finally:
            engine_globals["MAX_EVIDENCE_BYTES"] = original_limit
        if output_failed != [0] or output_state["status"] != "failed":
            fail("output limit did not finalize the mission as failed")
        output_evidence = json.loads(
            evidence_file(root, output_state["verification"][0]["evidence"]).read_text(
                encoding="utf-8"
            )
        )
        if output_evidence["output_limit_exceeded"] is not True:
            fail("output-limit evidence did not retain the limit violation")

        create_mission(
            root,
            "tamper-state",
            "Reject state edits outside the journal",
            ["State remains journaled"],
            artifacts=["drift.txt"],
            actor="validator",
        )
        record_artifacts(root, "tamper-state", "validator")
        tamper_paths = mission_paths(root, "tamper-state")
        tampered = json.loads(tamper_paths["state"].read_text(encoding="utf-8"))
        tampered["artifacts"][0]["required"] = False
        tamper_paths["state"].write_text(
            json.dumps(tampered, indent=2) + "\n", encoding="utf-8"
        )
        expect_mission_error(
            lambda: record_artifacts(root, "tamper-state", "validator"),
            "unjournaled state edit",
        )

        create_mission(
            root,
            "invalid-event",
            "Reject rehashed schema-invalid events",
            ["Event schema remains closed"],
            artifacts=["drift.txt"],
            actor="validator",
        )
        invalid_paths = mission_paths(root, "invalid-event")
        invalid_events = read_events(invalid_paths["events"], "invalid-event")
        invalid_event = dict(invalid_events[-1])
        invalid_event["event_type"] = "invented_event"
        invalid_event["event_hash"] = compute_event_hash(invalid_event)
        invalid_paths["events"].write_text(
            json.dumps(invalid_event, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        expect_mission_error(
            lambda: read_events(invalid_paths["events"], "invalid-event"),
            "schema-invalid rehashed event",
        )

        create_mission(
            root,
            "already-verifying",
            "Reject concurrent verification runs",
            ["Only one run executes"],
            verification=[passing],
            actor="validator",
        )
        verifying_paths = mission_paths(root, "already-verifying")
        with exclusive_lock(verifying_paths["lock"]):
            verifying_state, baseline_hash, _ = load_locked_state(
                root, "already-verifying", verifying_paths
            )
            verifying_state["status"] = "verifying"
            verifying_state["updated_at"] = module["utc_now"]()
            commit_locked(
                verifying_paths,
                verifying_state,
                "verification_started",
                "validator",
                {
                    "run_id": "mvr_manual",
                    "verification_count": 1,
                    "artifact_state_hash": module["artifact_snapshot_hash"](
                        verifying_state
                    ),
                },
                baseline_hash,
            )
        expect_mission_error(
            lambda: verify_mission(root, "already-verifying", "validator", timeout=30),
            "concurrent verification",
        )

        create_mission(
            root,
            "missing-artifact",
            "Return failure when required output is absent",
            ["Required artifact exists"],
            artifacts=["does-not-exist.txt"],
            actor="validator",
        )
        exit_code = main_cli(
            [
                "--root",
                str(root),
                "--actor",
                "validator",
                "record-artifacts",
                "missing-artifact",
            ]
        )
        if exit_code != 2:
            fail("record-artifacts returned success despite a missing required artifact")

        expect_mission_error(
            lambda: create_mission(
                root,
                "no-gates",
                "Reject prose-only missions",
                ["A prose criterion"],
                actor="validator",
            ),
            "prose-only mission",
        )
        expect_mission_error(
            lambda: parse_command("printf ok | cat"),
            "shell pipeline rejection",
        )

        live_lock = root / ".grok" / "runtime" / "missions" / "live.lock"
        live_lock.parent.mkdir(parents=True, exist_ok=True)
        live_lock.write_text(
            json.dumps(
                {"pid": os.getpid(), "token": "live-owner", "created_at": "old"}
            ),
            encoding="utf-8",
        )
        old = time.time() - 7200
        os.utime(live_lock, (old, old))
        expect_mission_error(
            lambda: _acquire_lock(exclusive_lock, live_lock),
            "live stale-looking lock",
        )
        if not live_lock.exists():
            fail("live mission lock was stolen")
        live_lock.unlink()

    source = (ROOT / "scripts" / "glaciereq-mission.py").read_text(encoding="utf-8")
    forbidden = ("shell=True", "os.system(", ".read_bytes()", "capture_output=True")
    for token in forbidden:
        if token in source:
            fail(f"mission engine contains forbidden execution pattern: {token}")
    for token in (
        "shell=False",
        "start_new_session",
        "os.killpg",
        "assert_state_matches_journal",
        "validate_evidence_reference",
        "output_limit_exceeded",
    ):
        if token not in source:
            fail(f"mission engine lost hardening mechanism: {token}")


def _acquire_lock(exclusive_lock: Any, path: Path) -> None:
    with exclusive_lock(path, timeout=0.15):
        pass


def main() -> int:
    schemas = load_schemas()
    server_names = validate_registry(schemas["connector-registry.schema.json"])
    validate_native_surfaces(server_names)
    validate_engine(schemas["mission-event.schema.json"])
    print("GlacierEQ Mission Control validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
