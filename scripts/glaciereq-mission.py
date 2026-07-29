#!/usr/bin/env python3
"""GlacierEQ Mission Control Plane v1.

Creates durable mission contracts, records artifact hashes, executes verification
commands without a shell, enforces completion gates, and maintains a hash-linked
mission event journal under ignored project runtime storage.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import time
from typing import Any, Iterator

MISSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
STATUSES = {
    "planned",
    "active",
    "blocked",
    "verifying",
    "complete",
    "failed",
    "cancelled",
}
LOCK_TIMEOUT_SECONDS = 10.0
STALE_LOCK_SECONDS = 3600.0
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "<<"}


class MissionError(RuntimeError):
    """Raised when mission state or an operation is invalid."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def hash_value(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_mission_id(mission_id: str) -> str:
    if MISSION_ID_RE.fullmatch(mission_id) is None:
        raise MissionError(
            "mission id must start with an alphanumeric character and contain only "
            "letters, digits, dots, underscores, or hyphens"
        )
    return mission_id


def repo_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


def mission_directory(root: Path, mission_id: str) -> Path:
    return root / ".grok" / "runtime" / "missions" / validate_mission_id(mission_id)


def mission_paths(root: Path, mission_id: str) -> dict[str, Path]:
    directory = mission_directory(root, mission_id)
    return {
        "directory": directory,
        "state": directory / "mission.json",
        "events": directory / "events.jsonl",
        "pending": directory / ".pending.json",
        "lock": directory / ".lock",
        "evidence": directory / "evidence",
    }


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{secrets.token_hex(16)}"
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, token.encode("utf-8"))
            os.fsync(descriptor)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
                if age > STALE_LOCK_SECONDS:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise MissionError(f"timed out acquiring mission lock: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if path.read_text(encoding="utf-8") == token:
                path.unlink()
        except FileNotFoundError:
            pass


def validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise MissionError("mission state must be an object")
    required = {
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
    }
    if set(state) != required:
        raise MissionError(
            f"mission state field drift: expected {sorted(required)}, found {sorted(state)}"
        )
    if state["schema_version"] != 1:
        raise MissionError("unsupported mission schema version")
    validate_mission_id(str(state["mission_id"]))
    if not isinstance(state["objective"], str) or not state["objective"].strip():
        raise MissionError("mission objective must be non-empty")
    if state["status"] not in STATUSES:
        raise MissionError(f"invalid mission status: {state['status']}")
    if not isinstance(state["completion_contract"], list) or not state["completion_contract"]:
        raise MissionError("mission requires at least one completion criterion")
    if not all(isinstance(item, str) and item.strip() for item in state["completion_contract"]):
        raise MissionError("completion criteria must be non-empty strings")
    for field in ("dependencies", "artifacts", "verification", "open_questions"):
        if not isinstance(state[field], list):
            raise MissionError(f"mission {field} must be an array")
    for artifact in state["artifacts"]:
        if set(artifact) != {"path", "required", "hash"}:
            raise MissionError("artifact contract drift")
        if not isinstance(artifact["path"], str) or not artifact["path"]:
            raise MissionError("artifact path must be non-empty")
        if not isinstance(artifact["required"], bool):
            raise MissionError("artifact required must be boolean")
        if artifact["hash"] is not None and HASH_RE.fullmatch(artifact["hash"]) is None:
            raise MissionError("artifact hash must be SHA-256")
    for check in state["verification"]:
        if set(check) != {"command", "required", "status", "evidence"}:
            raise MissionError("verification contract drift")
        if not isinstance(check["command"], str) or not check["command"].strip():
            raise MissionError("verification command must be non-empty")
        if check["status"] not in {"pending", "passed", "failed", "skipped"}:
            raise MissionError("invalid verification status")
    return state


def load_state(root: str | Path, mission_id: str) -> dict[str, Any]:
    root_path = repo_root(root)
    path = mission_paths(root_path, mission_id)["state"]
    if not path.exists():
        raise MissionError(f"mission does not exist: {mission_id}")
    return validate_state(json.loads(path.read_text(encoding="utf-8")))


def compute_event_hash(event: dict[str, Any]) -> str:
    core = dict(event)
    core.pop("event_hash", None)
    return hash_value(core)


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if raw and not raw.endswith("\n"):
        raise MissionError("mission event journal ends with a partial record")
    events: list[dict[str, Any]] = []
    parent: str | None = None
    identifiers: set[str] = set()
    for index, line in enumerate(raw.splitlines()):
        if not line:
            raise MissionError(f"mission event journal contains blank line {index + 1}")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise MissionError(f"invalid mission event JSON at line {index + 1}") from error
        if not isinstance(event, dict):
            raise MissionError("mission event must be an object")
        if event.get("parent_hash") != parent:
            raise MissionError(f"mission event parent mismatch at line {index + 1}")
        if event.get("event_hash") != compute_event_hash(event):
            raise MissionError(f"mission event hash mismatch at line {index + 1}")
        event_id = event.get("event_id")
        if event_id in identifiers:
            raise MissionError(f"duplicate mission event id: {event_id}")
        identifiers.add(str(event_id))
        events.append(event)
        parent = event["event_hash"]
    return events


def append_event(path: Path, event: dict[str, Any]) -> None:
    events = read_events(path)
    expected_parent = events[-1]["event_hash"] if events else None
    if event.get("parent_hash") != expected_parent:
        raise MissionError("event parent no longer matches journal head")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def make_event(
    mission_id: str,
    event_type: str,
    actor: str,
    metadata: dict[str, str | int | float | bool | None],
    parent_hash: str | None,
) -> dict[str, Any]:
    if not actor.strip():
        raise MissionError("event actor must be non-empty")
    event = {
        "schema_version": 1,
        "event_id": f"mse_{secrets.token_hex(12)}",
        "mission_id": mission_id,
        "event_type": event_type,
        "timestamp": utc_now(),
        "actor": actor,
        "metadata": metadata,
        "details_hash": hash_value(metadata),
        "parent_hash": parent_hash,
    }
    event["event_hash"] = compute_event_hash(event)
    return event


def recover_pending(paths: dict[str, Path]) -> None:
    pending_path = paths["pending"]
    if not pending_path.exists():
        return
    transaction = json.loads(pending_path.read_text(encoding="utf-8"))
    if set(transaction) != {"state", "event"}:
        raise MissionError("invalid pending mission transaction")
    state = validate_state(transaction["state"])
    event = transaction["event"]
    events = read_events(paths["events"])
    matching = [item for item in events if item.get("event_id") == event.get("event_id")]
    if not matching:
        append_event(paths["events"], event)
    elif events[-1].get("event_id") != event.get("event_id"):
        raise MissionError("pending transaction event is not the journal head")
    atomic_write_json(paths["state"], state)
    pending_path.unlink()


def commit_locked(
    paths: dict[str, Path],
    state: dict[str, Any],
    event_type: str,
    actor: str,
    metadata: dict[str, str | int | float | bool | None],
) -> dict[str, Any]:
    state = validate_state(state)
    events = read_events(paths["events"])
    parent = events[-1]["event_hash"] if events else None
    enriched = dict(metadata)
    enriched["state_hash"] = hash_value(state)
    event = make_event(state["mission_id"], event_type, actor, enriched, parent)
    atomic_write_json(paths["pending"], {"state": state, "event": event})
    append_event(paths["events"], event)
    atomic_write_json(paths["state"], state)
    paths["pending"].unlink()
    return event


def safe_artifact_path(root: Path, text: str) -> Path:
    candidate = Path(text)
    if candidate.is_absolute():
        raise MissionError(f"artifact path must be repository-relative: {text}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise MissionError(f"artifact escapes repository root: {text}") from error
    return resolved


def hash_artifact(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_file():
        return hash_bytes(path.read_bytes())
    if path.is_dir():
        entries: list[dict[str, str]] = []
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                raise MissionError(f"artifact directory contains symlink: {item}")
            if item.is_file():
                entries.append(
                    {
                        "path": item.relative_to(path).as_posix(),
                        "sha256": hash_bytes(item.read_bytes()),
                    }
                )
        return hash_value(entries)
    raise MissionError(f"unsupported artifact type: {path}")


def create_mission(
    root: str | Path,
    mission_id: str,
    objective: str,
    criteria: list[str],
    artifacts: list[str] | None = None,
    optional_artifacts: list[str] | None = None,
    verification: list[str] | None = None,
    optional_verification: list[str] | None = None,
    dependencies: list[str] | None = None,
    questions: list[str] | None = None,
    actor: str = "operator",
) -> dict[str, Any]:
    root_path = repo_root(root)
    mission_id = validate_mission_id(mission_id)
    paths = mission_paths(root_path, mission_id)
    paths["directory"].mkdir(parents=True, exist_ok=True)
    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        if paths["state"].exists() or paths["events"].exists():
            raise MissionError(f"mission already exists: {mission_id}")
        now = utc_now()
        state = {
            "schema_version": 1,
            "mission_id": mission_id,
            "objective": objective.strip(),
            "completion_contract": [item.strip() for item in criteria],
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "dependencies": list(dict.fromkeys(dependencies or [])),
            "artifacts": [
                {"path": path, "required": True, "hash": None}
                for path in artifacts or []
            ]
            + [
                {"path": path, "required": False, "hash": None}
                for path in optional_artifacts or []
            ],
            "verification": [
                {
                    "command": command,
                    "required": True,
                    "status": "pending",
                    "evidence": None,
                }
                for command in verification or []
            ]
            + [
                {
                    "command": command,
                    "required": False,
                    "status": "pending",
                    "evidence": None,
                }
                for command in optional_verification or []
            ],
            "open_questions": list(dict.fromkeys(questions or [])),
        }
        commit_locked(
            paths,
            state,
            "mission_created",
            actor,
            {
                "artifact_count": len(state["artifacts"]),
                "verification_count": len(state["verification"]),
            },
        )
        return state


def record_artifacts(
    root: str | Path, mission_id: str, actor: str = "operator"
) -> tuple[dict[str, Any], list[str]]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        state = load_state(root_path, mission_id)
        missing: list[str] = []
        for artifact in state["artifacts"]:
            artifact["hash"] = hash_artifact(
                safe_artifact_path(root_path, artifact["path"])
            )
            if artifact["required"] and artifact["hash"] is None:
                missing.append(artifact["path"])
        state["updated_at"] = utc_now()
        commit_locked(
            paths,
            state,
            "artifacts_recorded",
            actor,
            {
                "artifact_count": len(state["artifacts"]),
                "missing_required_count": len(missing),
                "missing_required": ",".join(missing) if missing else None,
            },
        )
        return state, missing


def parse_command(command: str) -> list[str]:
    if "`" in command or "$(" in command:
        raise MissionError("verification commands may not use shell substitution")
    argv = shlex.split(command)
    if not argv:
        raise MissionError("verification command is empty")
    if any(token in SHELL_TOKENS for token in argv):
        raise MissionError(
            "verification commands execute without a shell; split pipelines into separate checks"
        )
    return argv


def write_evidence(
    directory: Path,
    index: int,
    command: str,
    argv: list[str],
    started_at: str,
    finished_at: str,
    return_code: int | None,
    timed_out: bool,
    stdout: bytes,
    stderr: bytes,
) -> str:
    if len(stdout) + len(stderr) > MAX_EVIDENCE_BYTES:
        raise MissionError("verification output exceeds evidence size limit")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9]", "", finished_at)[:14]
    path = directory / f"verify-{index:03d}-{stamp}-{secrets.token_hex(4)}.json"
    evidence = {
        "schema_version": 1,
        "command_hash": hash_value(command),
        "executable": argv[0],
        "argument_count": max(0, len(argv) - 1),
        "started_at": started_at,
        "finished_at": finished_at,
        "return_code": return_code,
        "timed_out": timed_out,
        "stdout_bytes": len(stdout),
        "stdout_sha256": hash_bytes(stdout),
        "stderr_bytes": len(stderr),
        "stderr_sha256": hash_bytes(stderr),
        "raw_output_stored": False,
    }
    atomic_write_json(path, evidence)
    return path.as_posix()


def run_check(root: Path, evidence_dir: Path, index: int, command: str, timeout: int) -> tuple[bool, str]:
    argv = parse_command(command)
    started = utc_now()
    timed_out = False
    return_code: int | None
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            shell=False,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
        stdout = result.stdout
        stderr = result.stderr
        return_code = result.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        return_code = None
    finished = utc_now()
    evidence_path = write_evidence(
        evidence_dir,
        index,
        command,
        argv,
        started,
        finished,
        return_code,
        timed_out,
        stdout,
        stderr,
    )
    return (not timed_out and return_code == 0), evidence_path


def verify_mission(
    root: str | Path,
    mission_id: str,
    actor: str = "operator",
    timeout: int = 900,
) -> tuple[dict[str, Any], list[int]]:
    if timeout <= 0:
        raise MissionError("verification timeout must be positive")
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        state = load_state(root_path, mission_id)
        if state["status"] in {"complete", "cancelled"}:
            raise MissionError(f"cannot verify mission in status {state['status']}")
        state["status"] = "verifying"
        state["updated_at"] = utc_now()
        commit_locked(
            paths,
            state,
            "verification_started",
            actor,
            {"verification_count": len(state["verification"])},
        )

    results: list[tuple[bool, str]] = []
    for index, check in enumerate(state["verification"]):
        results.append(
            run_check(root_path, paths["evidence"], index, check["command"], timeout)
        )

    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        current = load_state(root_path, mission_id)
        if current["status"] != "verifying":
            raise MissionError("mission changed while verification was running")
        failed_required: list[int] = []
        for index, (passed, evidence_path) in enumerate(results):
            check = current["verification"][index]
            check["status"] = "passed" if passed else "failed"
            check["evidence"] = str(Path(evidence_path).relative_to(root_path))
            if check["required"] and not passed:
                failed_required.append(index)
        current["status"] = "failed" if failed_required else "active"
        current["updated_at"] = utc_now()
        commit_locked(
            paths,
            current,
            "verification_finished",
            actor,
            {
                "failed_required_count": len(failed_required),
                "failed_required": ",".join(map(str, failed_required))
                if failed_required
                else None,
            },
        )
        return current, failed_required


def completion_blockers(root: Path, state: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for artifact in state["artifacts"]:
        if artifact["required"] and artifact["hash"] is None:
            blockers.append(f"missing artifact: {artifact['path']}")
    for index, check in enumerate(state["verification"]):
        if check["required"] and check["status"] != "passed":
            blockers.append(f"verification {index} is {check['status']}")
    for question in state["open_questions"]:
        blockers.append(f"open question: {question}")
    for dependency in state["dependencies"]:
        try:
            dependency_state = load_state(root, dependency)
        except MissionError:
            blockers.append(f"missing dependency: {dependency}")
            continue
        if dependency_state["status"] != "complete":
            blockers.append(
                f"dependency {dependency} is {dependency_state['status']}"
            )
    return blockers


def complete_mission(
    root: str | Path, mission_id: str, actor: str = "operator"
) -> tuple[dict[str, Any], list[str]]:
    root_path = repo_root(root)
    record_artifacts(root_path, mission_id, actor)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        state = load_state(root_path, mission_id)
        blockers = completion_blockers(root_path, state)
        state["updated_at"] = utc_now()
        if blockers:
            state["status"] = "blocked"
            commit_locked(
                paths,
                state,
                "mission_blocked",
                actor,
                {
                    "blocker_count": len(blockers),
                    "blockers": " | ".join(blockers),
                },
            )
            return state, blockers
        state["status"] = "complete"
        commit_locked(
            paths,
            state,
            "mission_completed",
            actor,
            {"completion_criteria_count": len(state["completion_contract"])},
        )
        return state, []


def block_mission(root: str | Path, mission_id: str, reason: str, actor: str) -> dict[str, Any]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        state = load_state(root_path, mission_id)
        if state["status"] == "complete":
            raise MissionError("completed missions cannot be blocked")
        state["status"] = "blocked"
        state["updated_at"] = utc_now()
        commit_locked(paths, state, "mission_blocked", actor, {"reason": reason})
        return state


def resume_mission(root: str | Path, mission_id: str, actor: str) -> dict[str, Any]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        state = load_state(root_path, mission_id)
        if state["status"] != "blocked":
            raise MissionError("only blocked missions can be resumed")
        state["status"] = "active"
        state["updated_at"] = utc_now()
        commit_locked(paths, state, "mission_resumed", actor, {})
        return state


def audit_mission(
    root: str | Path,
    mission_id: str,
    actor: str = "operator",
    deep: bool = False,
) -> dict[str, Any]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        state = load_state(root_path, mission_id)
        events = read_events(paths["events"])
        if not events:
            raise MissionError("mission event journal is empty")
        if events[-1]["metadata"].get("state_hash") != hash_value(state):
            raise MissionError("mission state does not match the event journal head")
        missing_evidence: list[str] = []
        for check in state["verification"]:
            if check["evidence"] and not safe_artifact_path(
                root_path, check["evidence"]
            ).exists():
                missing_evidence.append(check["evidence"])
        drifted_artifacts: list[str] = []
        if deep:
            for artifact in state["artifacts"]:
                current_hash = hash_artifact(
                    safe_artifact_path(root_path, artifact["path"])
                )
                if artifact["hash"] is not None and current_hash != artifact["hash"]:
                    drifted_artifacts.append(artifact["path"])
        if missing_evidence or drifted_artifacts:
            raise MissionError(
                "mission audit failed: "
                + "; ".join(
                    filter(
                        None,
                        [
                            f"missing evidence {missing_evidence}"
                            if missing_evidence
                            else "",
                            f"artifact drift {drifted_artifacts}"
                            if drifted_artifacts
                            else "",
                        ],
                    )
                )
            )
        commit_locked(
            paths,
            state,
            "mission_audited",
            actor,
            {"deep": deep, "event_count_before_audit": len(events)},
        )
        return {
            "mission_id": mission_id,
            "status": state["status"],
            "event_count": len(events) + 1,
            "deep": deep,
            "ok": True,
        }


def summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "mission_id": state["mission_id"],
        "objective": state["objective"],
        "status": state["status"],
        "required_artifacts": sum(1 for item in state["artifacts"] if item["required"]),
        "recorded_artifacts": sum(1 for item in state["artifacts"] if item["hash"]),
        "required_checks": sum(1 for item in state["verification"] if item["required"]),
        "passed_checks": sum(1 for item in state["verification"] if item["status"] == "passed"),
        "open_questions": len(state["open_questions"]),
        "updated_at": state["updated_at"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", default=".", help="repository root")
    result.add_argument(
        "--actor", default=os.environ.get("GLACIEREQ_ACTOR", "operator")
    )
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a mission")
    init.add_argument("mission_id")
    init.add_argument("--objective", required=True)
    init.add_argument("--criterion", action="append", required=True)
    init.add_argument("--artifact", action="append", default=[])
    init.add_argument("--optional-artifact", action="append", default=[])
    init.add_argument("--verification", action="append", default=[])
    init.add_argument("--optional-verification", action="append", default=[])
    init.add_argument("--dependency", action="append", default=[])
    init.add_argument("--question", action="append", default=[])

    for name in ("show", "record-artifacts", "complete", "resume"):
        command = commands.add_parser(name)
        command.add_argument("mission_id")

    verify = commands.add_parser("verify")
    verify.add_argument("mission_id")
    verify.add_argument("--timeout", type=int, default=900)

    block = commands.add_parser("block")
    block.add_argument("mission_id")
    block.add_argument("--reason", required=True)

    audit = commands.add_parser("audit")
    audit.add_argument("mission_id")
    audit.add_argument("--deep", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            state = create_mission(
                args.root,
                args.mission_id,
                args.objective,
                args.criterion,
                args.artifact,
                args.optional_artifact,
                args.verification,
                args.optional_verification,
                args.dependency,
                args.question,
                args.actor,
            )
            output: Any = summary(state)
        elif args.command == "show":
            output = load_state(args.root, args.mission_id)
        elif args.command == "record-artifacts":
            state, missing = record_artifacts(args.root, args.mission_id, args.actor)
            output = {"mission": summary(state), "missing_required": missing}
        elif args.command == "verify":
            state, failed = verify_mission(
                args.root, args.mission_id, args.actor, args.timeout
            )
            output = {"mission": summary(state), "failed_required": failed}
        elif args.command == "complete":
            state, blockers = complete_mission(args.root, args.mission_id, args.actor)
            output = {"mission": summary(state), "blockers": blockers}
        elif args.command == "block":
            output = summary(
                block_mission(
                    args.root, args.mission_id, args.reason, args.actor
                )
            )
        elif args.command == "resume":
            output = summary(resume_mission(args.root, args.mission_id, args.actor))
        elif args.command == "audit":
            output = audit_mission(
                args.root, args.mission_id, args.actor, args.deep
            )
        else:
            raise MissionError(f"unsupported command: {args.command}")
    except MissionError as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2, sort_keys=True))
    if isinstance(output, dict) and output.get("blockers"):
        return 2
    if isinstance(output, dict) and output.get("failed_required"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
