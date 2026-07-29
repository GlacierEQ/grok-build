#!/usr/bin/env python3
"""GlacierEQ Mission Control Plane v1.

Creates durable mission contracts, records streaming artifact hashes, executes
bounded verification commands without a shell, enforces explicit completion
criteria, and maintains a recoverable hash-linked event journal under ignored
project runtime storage.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

MISSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EVENT_ID_RE = re.compile(r"^mse_[a-f0-9]{24}$")
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
EVENT_TYPES = {
    "mission_created",
    "status_changed",
    "artifacts_recorded",
    "verification_started",
    "verification_finished",
    "criterion_satisfied",
    "question_resolved",
    "mission_completed",
    "mission_blocked",
    "mission_resumed",
    "mission_audited",
}
STATE_REQUIRED = {
    "schema_version",
    "mission_id",
    "objective",
    "completion_contract",
    "status",
    "created_at",
    "artifacts",
    "verification",
}
STATE_ALLOWED = STATE_REQUIRED | {
    "updated_at",
    "dependencies",
    "open_questions",
}
ARTIFACT_REQUIRED = {"path", "required"}
ARTIFACT_ALLOWED = ARTIFACT_REQUIRED | {"hash"}
VERIFICATION_REQUIRED = {"command", "required"}
VERIFICATION_ALLOWED = VERIFICATION_REQUIRED | {"status", "evidence"}
EVENT_FIELDS = {
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
}
LOCK_TIMEOUT_SECONDS = 10.0
STALE_LOCK_SECONDS = 60.0
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
POLL_INTERVAL_SECONDS = 0.05
SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "<<"}


class MissionError(RuntimeError):
    """Raised when mission state or an operation is invalid."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise MissionError(f"{label} must be a date-time string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MissionError(f"{label} is not a valid date-time") from error
    if parsed.tzinfo is None:
        raise MissionError(f"{label} must include a timezone")


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def hash_value(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_hash(value: Any, label: str, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise MissionError(f"{label} must be a SHA-256 digest")


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


def fsync_directory(directory: Path) -> None:
    """Persist a directory entry on platforms that support directory fsync."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise MissionError(f"failed writing temporary file: {temporary}")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, payload.encode("utf-8"))


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        return True
    return True


def read_lock_owner(path: Path) -> tuple[int | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None
    if not isinstance(value, dict):
        return None, None
    pid = value.get("pid")
    token = value.get("token")
    return (pid if isinstance(pid, int) else None, token if isinstance(token, str) else None)


@contextmanager
def exclusive_lock(
    path: Path, timeout: float = LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Acquire a cross-platform lock without stealing it from a live owner."""

    if timeout <= 0:
        raise MissionError("lock timeout must be positive")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    owner = {"pid": os.getpid(), "token": token, "created_at": utc_now()}
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, canonical(owner))
            os.fsync(descriptor)
            fsync_directory(path.parent)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            owner_pid, _ = read_lock_owner(path)
            owner_is_live = owner_pid is not None and process_alive(owner_pid)
            if not owner_is_live and age > STALE_LOCK_SECONDS:
                try:
                    path.unlink()
                    fsync_directory(path.parent)
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                owner_label = str(owner_pid) if owner_pid is not None else "unknown"
                raise MissionError(
                    f"timed out acquiring mission lock owned by pid {owner_label}: {path}"
                )
            time.sleep(POLL_INTERVAL_SECONDS)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            current_pid, current_token = read_lock_owner(path)
            if current_pid == os.getpid() and current_token == token:
                path.unlink()
                fsync_directory(path.parent)
        except FileNotFoundError:
            pass


def normalize_state(value: Any) -> dict[str, Any]:
    """Accept every published-schema-valid shape and normalize runtime defaults."""

    if not isinstance(value, dict):
        raise MissionError("mission state must be an object")
    fields = set(value)
    missing = STATE_REQUIRED - fields
    unexpected = fields - STATE_ALLOWED
    if missing:
        raise MissionError(f"mission state is missing fields: {sorted(missing)}")
    if unexpected:
        raise MissionError(f"mission state has unexpected fields: {sorted(unexpected)}")
    state = dict(value)
    state.setdefault("updated_at", None)
    state.setdefault("dependencies", [])
    state.setdefault("open_questions", [])
    if state["schema_version"] != 1:
        raise MissionError("unsupported mission schema version")
    state["mission_id"] = validate_mission_id(str(state["mission_id"]))
    if not isinstance(state["objective"], str) or not state["objective"].strip():
        raise MissionError("mission objective must be non-empty")
    if state["status"] not in STATUSES:
        raise MissionError(f"invalid mission status: {state['status']}")
    parse_datetime(state["created_at"], "created_at")
    if state["updated_at"] is not None:
        parse_datetime(state["updated_at"], "updated_at")
    criteria = state["completion_contract"]
    if not isinstance(criteria, list) or not criteria:
        raise MissionError("mission requires at least one completion criterion")
    if not all(isinstance(item, str) and item.strip() for item in criteria):
        raise MissionError("completion criteria must be non-empty strings")
    for field in ("dependencies", "artifacts", "verification", "open_questions"):
        if not isinstance(state[field], list):
            raise MissionError(f"mission {field} must be an array")
    if not all(isinstance(item, str) and item for item in state["dependencies"]):
        raise MissionError("mission dependencies must be non-empty strings")
    if len(set(state["dependencies"])) != len(state["dependencies"]):
        raise MissionError("mission dependencies must be unique")
    if not all(isinstance(item, str) and item for item in state["open_questions"]):
        raise MissionError("open questions must be non-empty strings")

    artifacts: list[dict[str, Any]] = []
    for raw in state["artifacts"]:
        if not isinstance(raw, dict):
            raise MissionError("artifact entry must be an object")
        fields = set(raw)
        if ARTIFACT_REQUIRED - fields or fields - ARTIFACT_ALLOWED:
            raise MissionError("artifact contract drift")
        artifact = dict(raw)
        artifact.setdefault("hash", None)
        if not isinstance(artifact["path"], str) or not artifact["path"]:
            raise MissionError("artifact path must be non-empty")
        if not isinstance(artifact["required"], bool):
            raise MissionError("artifact required must be boolean")
        validate_hash(artifact["hash"], "artifact hash", nullable=True)
        artifacts.append(artifact)
    state["artifacts"] = artifacts

    checks: list[dict[str, Any]] = []
    for raw in state["verification"]:
        if not isinstance(raw, dict):
            raise MissionError("verification entry must be an object")
        fields = set(raw)
        if VERIFICATION_REQUIRED - fields or fields - VERIFICATION_ALLOWED:
            raise MissionError("verification contract drift")
        check = dict(raw)
        check.setdefault("status", "pending")
        check.setdefault("evidence", None)
        if not isinstance(check["command"], str) or not check["command"].strip():
            raise MissionError("verification command must be non-empty")
        if not isinstance(check["required"], bool):
            raise MissionError("verification required must be boolean")
        if check["status"] not in {"pending", "passed", "failed", "skipped"}:
            raise MissionError("invalid verification status")
        if check["evidence"] is not None and not isinstance(check["evidence"], str):
            raise MissionError("verification evidence must be a string reference or null")
        checks.append(check)
    state["verification"] = checks
    return state


def load_state(root: str | Path, mission_id: str) -> dict[str, Any]:
    root_path = repo_root(root)
    path = mission_paths(root_path, mission_id)["state"]
    if not path.exists():
        raise MissionError(f"mission does not exist: {mission_id}")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise MissionError(f"mission state is invalid JSON: {mission_id}") from error
    return normalize_state(parsed)


def compute_event_hash(event: dict[str, Any]) -> str:
    core = dict(event)
    core.pop("event_hash", None)
    return hash_value(core)


def event_line(event: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            event,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def validate_event(
    event: Any,
    expected_parent: str | None,
    expected_mission_id: str | None,
) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise MissionError("mission event does not match the closed event contract")
    if event["schema_version"] != 1:
        raise MissionError("unsupported mission event schema version")
    if not isinstance(event["event_id"], str) or EVENT_ID_RE.fullmatch(event["event_id"]) is None:
        raise MissionError("mission event id is invalid")
    validate_mission_id(str(event["mission_id"]))
    if expected_mission_id is not None and event["mission_id"] != expected_mission_id:
        raise MissionError("mission event references a different mission")
    if event["event_type"] not in EVENT_TYPES:
        raise MissionError(f"invalid mission event type: {event['event_type']}")
    parse_datetime(event["timestamp"], "mission event timestamp")
    if not isinstance(event["actor"], str) or not event["actor"].strip():
        raise MissionError("mission event actor must be non-empty")
    metadata = event["metadata"]
    if not isinstance(metadata, dict):
        raise MissionError("mission event metadata must be an object")
    if not all(
        isinstance(key, str)
        and (value is None or isinstance(value, (str, int, float, bool)))
        for key, value in metadata.items()
    ):
        raise MissionError("mission event metadata values must be scalar")
    validate_hash(event["details_hash"], "mission event details hash")
    if event["details_hash"] != hash_value(metadata):
        raise MissionError("mission event details hash mismatch")
    validate_hash(event["parent_hash"], "mission event parent hash", nullable=True)
    if event["parent_hash"] != expected_parent:
        raise MissionError("mission event parent hash mismatch")
    validate_hash(event["event_hash"], "mission event hash")
    if event["event_hash"] != compute_event_hash(event):
        raise MissionError("mission event hash mismatch")
    validate_hash(metadata.get("state_hash"), "mission event state hash")
    return event


def read_events(
    path: Path, expected_mission_id: str | None = None
) -> list[dict[str, Any]]:
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
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise MissionError(f"invalid mission event JSON at line {index + 1}") from error
        event = validate_event(parsed, parent, expected_mission_id)
        if event["event_id"] in identifiers:
            raise MissionError(f"duplicate mission event id: {event['event_id']}")
        identifiers.add(event["event_id"])
        events.append(event)
        parent = event["event_hash"]
    return events


def append_event(path: Path, event: dict[str, Any], mission_id: str) -> None:
    events = read_events(path, mission_id)
    expected_parent = events[-1]["event_hash"] if events else None
    validate_event(event, expected_parent, mission_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        remaining = memoryview(event_line(event))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise MissionError("failed appending mission event")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def make_event(
    mission_id: str,
    event_type: str,
    actor: str,
    metadata: dict[str, str | int | float | bool | None],
    parent_hash: str | None,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise MissionError(f"unsupported mission event type: {event_type}")
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
    validate_event(event, parent_hash, mission_id)
    return event


def repair_torn_event_tail(path: Path, pending_event: dict[str, Any]) -> None:
    """Remove only a partial tail proven to be a prefix of the pending event."""

    if not path.exists():
        return
    raw = path.read_bytes()
    if not raw or raw.endswith(b"\n"):
        return
    prefix_end = raw.rfind(b"\n") + 1
    prefix = raw[:prefix_end]
    tail = raw[prefix_end:]
    expected = event_line(pending_event)[:-1]
    if not expected.startswith(tail):
        raise MissionError("torn journal tail does not match the pending transaction")
    atomic_write_bytes(path, prefix)


def recover_pending(paths: dict[str, Path]) -> None:
    pending_path = paths["pending"]
    if not pending_path.exists():
        return
    try:
        transaction = json.loads(pending_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise MissionError("pending mission transaction is invalid JSON") from error
    if not isinstance(transaction, dict) or set(transaction) != {"state", "event"}:
        raise MissionError("invalid pending mission transaction")
    state = normalize_state(transaction["state"])
    event = transaction["event"]
    validate_event(event, event.get("parent_hash"), state["mission_id"])
    if event["metadata"].get("state_hash") != hash_value(state):
        raise MissionError("pending transaction state hash mismatch")
    repair_torn_event_tail(paths["events"], event)
    events = read_events(paths["events"], state["mission_id"])
    parent = events[-1]["event_hash"] if events else None
    matching = [item for item in events if item["event_id"] == event["event_id"]]
    if not matching:
        if event["parent_hash"] != parent:
            raise MissionError("pending transaction parent no longer matches journal")
        append_event(paths["events"], event, state["mission_id"])
    elif (
        events[-1]["event_id"] != event["event_id"]
        or events[-1]["event_hash"] != event["event_hash"]
    ):
        raise MissionError("pending transaction event is not the journal head")
    atomic_write_json(paths["state"], state)
    pending_path.unlink()
    fsync_directory(pending_path.parent)


def assert_state_matches_journal(
    paths: dict[str, Path], state: dict[str, Any]
) -> list[dict[str, Any]]:
    events = read_events(paths["events"], state["mission_id"])
    if not events:
        raise MissionError("mission state has no event journal")
    if events[-1]["metadata"].get("state_hash") != hash_value(state):
        raise MissionError("mission state was changed outside the event journal")
    return events


def load_locked_state(
    root: Path, mission_id: str, paths: dict[str, Path]
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    recover_pending(paths)
    state = load_state(root, mission_id)
    events = assert_state_matches_journal(paths, state)
    return state, hash_value(state), events


def commit_locked(
    paths: dict[str, Path],
    state: dict[str, Any],
    event_type: str,
    actor: str,
    metadata: dict[str, str | int | float | bool | None],
    expected_state_hash: str | None,
) -> dict[str, Any]:
    state = normalize_state(state)
    events = read_events(paths["events"], state["mission_id"])
    if events:
        if not paths["state"].exists():
            raise MissionError("mission journal exists without state")
        try:
            current_raw = json.loads(paths["state"].read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise MissionError("mission state became invalid JSON") from error
        current = normalize_state(current_raw)
        current_hash = hash_value(current)
        if events[-1]["metadata"].get("state_hash") != current_hash:
            raise MissionError("mission state changed outside the event journal")
        if expected_state_hash is not None and current_hash != expected_state_hash:
            raise MissionError("mission state changed during the operation")
    elif paths["state"].exists():
        raise MissionError("mission state exists without an event journal")
    elif expected_state_hash is not None:
        raise MissionError("expected mission baseline is missing")

    parent = events[-1]["event_hash"] if events else None
    enriched = dict(metadata)
    enriched["state_hash"] = hash_value(state)
    event = make_event(state["mission_id"], event_type, actor, enriched, parent)
    atomic_write_json(paths["pending"], {"state": state, "event": event})
    append_event(paths["events"], event, state["mission_id"])
    atomic_write_json(paths["state"], state)
    paths["pending"].unlink()
    fsync_directory(paths["pending"].parent)
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
    if path.is_symlink():
        raise MissionError(f"artifact is a symlink: {path}")
    if path.is_file():
        return hash_file(path)
    if path.is_dir():
        digest = hashlib.sha256()
        for current_root, directory_names, file_names in os.walk(path):
            directory_names.sort()
            file_names.sort()
            current = Path(current_root)
            for directory_name in directory_names:
                directory = current / directory_name
                if directory.is_symlink():
                    raise MissionError(f"artifact directory contains symlink: {directory}")
            for file_name in file_names:
                item = current / file_name
                if item.is_symlink():
                    raise MissionError(f"artifact directory contains symlink: {item}")
                relative = item.relative_to(path).as_posix().encode("utf-8")
                file_hash = hash_file(item).encode("ascii")
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                digest.update(file_hash)
        return digest.hexdigest()
    raise MissionError(f"unsupported artifact type: {path}")


def artifact_snapshot_hash(state: dict[str, Any]) -> str:
    return hash_value(
        [
            {
                "path": artifact["path"],
                "required": artifact["required"],
                "hash": artifact["hash"],
            }
            for artifact in state["artifacts"]
        ]
    )


def artifact_drift(root: Path, state: dict[str, Any]) -> list[str]:
    drifted: list[str] = []
    for artifact in state["artifacts"]:
        current_hash = hash_artifact(safe_artifact_path(root, artifact["path"]))
        if current_hash != artifact["hash"]:
            drifted.append(artifact["path"])
    return drifted


def parse_evidence_reference(reference: str) -> tuple[str, str]:
    path_text, separator, digest = reference.rpartition("#")
    if not separator or not path_text or HASH_RE.fullmatch(digest) is None:
        raise MissionError("verification evidence reference is malformed")
    return path_text, digest


def validate_evidence_reference(
    root: Path, reference: str, expected_artifact_snapshot: str | None = None
) -> dict[str, Any]:
    path_text, expected_hash = parse_evidence_reference(reference)
    path = safe_artifact_path(root, path_text)
    if not path.is_file():
        raise MissionError(f"verification evidence is missing: {path_text}")
    if hash_file(path) != expected_hash:
        raise MissionError(f"verification evidence hash mismatch: {path_text}")
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise MissionError(f"verification evidence is invalid JSON: {path_text}") from error
    required = {
        "schema_version",
        "run_id",
        "check_index",
        "command_hash",
        "executable",
        "argument_count",
        "artifact_state_hash",
        "started_at",
        "finished_at",
        "return_code",
        "timed_out",
        "output_limit_exceeded",
        "launch_error_type",
        "launch_errno",
        "internal_error_type",
        "stdout_bytes",
        "stdout_sha256",
        "stderr_bytes",
        "stderr_sha256",
        "raw_output_stored",
    }
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise MissionError(f"verification evidence contract drift: {path_text}")
    if evidence["schema_version"] != 1 or evidence["raw_output_stored"] is not False:
        raise MissionError(f"verification evidence policy violation: {path_text}")
    for field in ("command_hash", "artifact_state_hash", "stdout_sha256", "stderr_sha256"):
        validate_hash(evidence[field], f"verification evidence {field}")
    if expected_artifact_snapshot is not None and evidence["artifact_state_hash"] != expected_artifact_snapshot:
        raise MissionError("verification evidence was produced for different artifacts")
    return evidence


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
    required_artifacts = artifacts or []
    required_verification = verification or []
    if not required_artifacts and not required_verification:
        raise MissionError(
            "mission requires at least one required artifact or required verification gate"
        )
    paths = mission_paths(root_path, mission_id)
    paths["directory"].mkdir(parents=True, exist_ok=True)
    with exclusive_lock(paths["lock"]):
        recover_pending(paths)
        if paths["state"].exists() or paths["events"].exists():
            raise MissionError(f"mission already exists: {mission_id}")
        now = utc_now()
        state = normalize_state(
            {
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
                    for path in required_artifacts
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
                    for command in required_verification
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
        )
        commit_locked(
            paths,
            state,
            "mission_created",
            actor,
            {
                "artifact_count": len(state["artifacts"]),
                "verification_count": len(state["verification"]),
                "criterion_count": len(state["completion_contract"]),
            },
            None,
        )
        return state


def reject_mutation_status(state: dict[str, Any], operation: str) -> None:
    if state["status"] in {"blocked", "complete", "cancelled", "verifying"}:
        raise MissionError(f"cannot {operation} mission in status {state['status']}")


def record_artifacts(
    root: str | Path, mission_id: str, actor: str = "operator"
) -> tuple[dict[str, Any], list[str]]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        state, baseline_hash, _ = load_locked_state(root_path, mission_id, paths)
        reject_mutation_status(state, "record artifacts for")
        missing: list[str] = []
        changed: list[str] = []
        for artifact in state["artifacts"]:
            previous = artifact["hash"]
            current = hash_artifact(safe_artifact_path(root_path, artifact["path"]))
            artifact["hash"] = current
            if previous != current:
                changed.append(artifact["path"])
            if artifact["required"] and current is None:
                missing.append(artifact["path"])
        if changed:
            for check in state["verification"]:
                check["status"] = "pending"
                check["evidence"] = None
        state["updated_at"] = utc_now()
        commit_locked(
            paths,
            state,
            "artifacts_recorded",
            actor,
            {
                "artifact_count": len(state["artifacts"]),
                "artifact_state_hash": artifact_snapshot_hash(state),
                "changed_count": len(changed),
                "missing_required_count": len(missing),
                "verification_invalidated": bool(changed),
            },
            baseline_hash,
        )
        return state, missing


def parse_command(command: str) -> list[str]:
    if "`" in command or "$(" in command:
        raise MissionError("verification commands may not use shell substitution")
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise MissionError("verification command has invalid quoting") from error
    if not argv:
        raise MissionError("verification command is empty")
    if any(token in SHELL_TOKENS for token in argv):
        raise MissionError(
            "verification commands execute without a shell; split pipelines into separate checks"
        )
    return argv


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
    else:
        process.terminate()
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise MissionError("verification process group could not be terminated") from error


def write_evidence_record(
    root: Path,
    directory: Path,
    evidence: dict[str, Any],
) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9]", "", evidence["finished_at"])[:14]
    path = directory / (
        f"verify-{evidence['check_index']:03d}-{stamp}-{secrets.token_hex(4)}.json"
    )
    atomic_write_json(path, evidence)
    relative = path.relative_to(root).as_posix()
    return f"{relative}#{hash_file(path)}"


def empty_hash() -> str:
    return hashlib.sha256(b"").hexdigest()


def run_check(
    root: Path,
    evidence_dir: Path,
    index: int,
    command: str,
    timeout: int,
    run_id: str,
    artifact_state_hash: str,
) -> tuple[bool, str]:
    started = utc_now()
    argv: list[str] = []
    process: subprocess.Popen[bytes] | None = None
    return_code: int | None = None
    timed_out = False
    output_limit_exceeded = False
    launch_error_type: str | None = None
    launch_errno: int | None = None
    internal_error_type: str | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    stdout_size = 0
    stderr_size = 0
    stdout_hash = empty_hash()
    stderr_hash = empty_hash()

    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        argv = parse_command(command)
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix="stdout-", dir=evidence_dir, delete=False
        ) as stdout_handle, tempfile.NamedTemporaryFile(
            mode="w+b", prefix="stderr-", dir=evidence_dir, delete=False
        ) as stderr_handle:
            stdout_path = Path(stdout_handle.name)
            stderr_path = Path(stderr_handle.name)
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            try:
                # The command is an explicit operator-authored argv contract. It is
                # parsed without a shell; shell metacharacters/substitution are rejected.
                process = subprocess.Popen(  # nosec B603
                    argv,
                    cwd=root,
                    shell=False,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=os.name == "posix",
                    creationflags=creationflags,
                )
            except OSError as error:
                launch_error_type = error.__class__.__name__
                launch_errno = error.errno
            if process is not None:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    stdout_handle.flush()
                    stderr_handle.flush()
                    stdout_size = os.fstat(stdout_handle.fileno()).st_size
                    stderr_size = os.fstat(stderr_handle.fileno()).st_size
                    if stdout_size + stderr_size > MAX_EVIDENCE_BYTES:
                        output_limit_exceeded = True
                        terminate_process_group(process)
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        terminate_process_group(process)
                        break
                    time.sleep(POLL_INTERVAL_SECONDS)
                if process.poll() is None:
                    terminate_process_group(process)
                return_code = process.wait()
            stdout_handle.flush()
            stderr_handle.flush()
            stdout_size = os.fstat(stdout_handle.fileno()).st_size
            stderr_size = os.fstat(stderr_handle.fileno()).st_size
            if stdout_size + stderr_size > MAX_EVIDENCE_BYTES:
                output_limit_exceeded = True
    except MissionError as error:
        launch_error_type = error.__class__.__name__
    except Exception as error:  # preserve a terminal failed result for unexpected runtime faults
        internal_error_type = error.__class__.__name__
        if process is not None and process.poll() is None:
            terminate_process_group(process)
    finally:
        if stdout_path is not None and stdout_path.exists():
            stdout_size = stdout_path.stat().st_size
            stdout_hash = hash_file(stdout_path)
        if stderr_path is not None and stderr_path.exists():
            stderr_size = stderr_path.stat().st_size
            stderr_hash = hash_file(stderr_path)
        for path in (stdout_path, stderr_path):
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    finished = utc_now()
    passed = (
        return_code == 0
        and not timed_out
        and not output_limit_exceeded
        and launch_error_type is None
        and internal_error_type is None
    )
    evidence = {
        "schema_version": 1,
        "run_id": run_id,
        "check_index": index,
        "command_hash": hash_value(command),
        "executable": argv[0] if argv else None,
        "argument_count": max(0, len(argv) - 1),
        "artifact_state_hash": artifact_state_hash,
        "started_at": started,
        "finished_at": finished,
        "return_code": return_code,
        "timed_out": timed_out,
        "output_limit_exceeded": output_limit_exceeded,
        "launch_error_type": launch_error_type,
        "launch_errno": launch_errno,
        "internal_error_type": internal_error_type,
        "stdout_bytes": stdout_size,
        "stdout_sha256": stdout_hash,
        "stderr_bytes": stderr_size,
        "stderr_sha256": stderr_hash,
        "raw_output_stored": False,
    }
    return passed, write_evidence_record(root, evidence_dir, evidence)


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

    recorded_state, missing = record_artifacts(root_path, mission_id, actor)
    if missing:
        raise MissionError(f"required artifacts are missing: {missing}")
    if not recorded_state["verification"]:
        raise MissionError("mission has no verification checks")

    run_id = f"mvr_{secrets.token_hex(12)}"
    with exclusive_lock(paths["lock"]):
        state, baseline_hash, _ = load_locked_state(root_path, mission_id, paths)
        if state["status"] in {"blocked", "complete", "cancelled", "verifying"}:
            raise MissionError(f"cannot verify mission in status {state['status']}")
        snapshot = artifact_snapshot_hash(state)
        for check in state["verification"]:
            check["status"] = "pending"
            check["evidence"] = None
        state["status"] = "verifying"
        state["updated_at"] = utc_now()
        commit_locked(
            paths,
            state,
            "verification_started",
            actor,
            {
                "verification_count": len(state["verification"]),
                "run_id": run_id,
                "artifact_state_hash": snapshot,
            },
            baseline_hash,
        )

    results = [
        run_check(
            root_path,
            paths["evidence"],
            index,
            check["command"],
            timeout,
            run_id,
            snapshot,
        )
        for index, check in enumerate(state["verification"])
    ]

    with exclusive_lock(paths["lock"]):
        current, baseline_hash, events = load_locked_state(root_path, mission_id, paths)
        if current["status"] != "verifying":
            raise MissionError("mission changed while verification was running")
        starts = [
            event
            for event in events
            if event["event_type"] == "verification_started"
            and event["metadata"].get("run_id") == run_id
        ]
        finishes = [
            event
            for event in events
            if event["event_type"] == "verification_finished"
            and event["metadata"].get("run_id") == run_id
        ]
        if len(starts) != 1 or finishes:
            raise MissionError("verification run identity is inconsistent")
        drifted = artifact_drift(root_path, current)
        failed_required: list[int] = []
        for index, (passed, evidence_reference) in enumerate(results):
            check = current["verification"][index]
            check_passed = passed and not drifted
            check["status"] = "passed" if check_passed else "failed"
            check["evidence"] = evidence_reference
            if check["required"] and not check_passed:
                failed_required.append(index)
        current["status"] = "failed" if failed_required or drifted else "active"
        current["updated_at"] = utc_now()
        commit_locked(
            paths,
            current,
            "verification_finished",
            actor,
            {
                "run_id": run_id,
                "artifact_state_hash": snapshot,
                "artifact_drift_count": len(drifted),
                "failed_required_count": len(failed_required),
            },
            baseline_hash,
        )
        return current, failed_required


def satisfy_criterion(
    root: str | Path,
    mission_id: str,
    index: int,
    evidence: str,
    actor: str = "operator",
) -> dict[str, Any]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    if not evidence.strip():
        raise MissionError("criterion evidence reference must be non-empty")
    with exclusive_lock(paths["lock"]):
        state, baseline_hash, _ = load_locked_state(root_path, mission_id, paths)
        reject_mutation_status(state, "satisfy a criterion for")
        if index < 0 or index >= len(state["completion_contract"]):
            raise MissionError("criterion index is out of range")
        criterion = state["completion_contract"][index]
        state["updated_at"] = utc_now()
        commit_locked(
            paths,
            state,
            "criterion_satisfied",
            actor,
            {
                "criterion_index": index,
                "criterion_hash": hash_value(criterion),
                "evidence_hash": hash_value(evidence),
            },
            baseline_hash,
        )
        return state


def resolve_question(
    root: str | Path,
    mission_id: str,
    index: int,
    answer: str,
    actor: str = "operator",
) -> dict[str, Any]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    if not answer.strip():
        raise MissionError("question answer must be non-empty")
    with exclusive_lock(paths["lock"]):
        state, baseline_hash, _ = load_locked_state(root_path, mission_id, paths)
        reject_mutation_status(state, "resolve a question for")
        if index < 0 or index >= len(state["open_questions"]):
            raise MissionError("question index is out of range")
        question = state["open_questions"].pop(index)
        state["updated_at"] = utc_now()
        commit_locked(
            paths,
            state,
            "question_resolved",
            actor,
            {
                "question_index": index,
                "question_hash": hash_value(question),
                "answer_hash": hash_value(answer),
            },
            baseline_hash,
        )
        return state


def satisfied_criteria(
    state: dict[str, Any], events: list[dict[str, Any]]
) -> set[int]:
    satisfied: set[int] = set()
    for event in events:
        if event["event_type"] != "criterion_satisfied":
            continue
        index = event["metadata"].get("criterion_index")
        criterion_hash = event["metadata"].get("criterion_hash")
        if (
            isinstance(index, int)
            and 0 <= index < len(state["completion_contract"])
            and criterion_hash == hash_value(state["completion_contract"][index])
        ):
            satisfied.add(index)
    return satisfied


def load_consistent_dependency(root: Path, mission_id: str) -> dict[str, Any]:
    paths = mission_paths(root, mission_id)
    state = load_state(root, mission_id)
    assert_state_matches_journal(paths, state)
    return state


def latest_block_reference(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event["event_type"] == "mission_resumed":
            return None
        if event["event_type"] == "mission_blocked":
            metadata = event["metadata"]
            value = metadata.get("reason_hash") or metadata.get("blockers_hash")
            return str(value) if value else event["event_hash"]
    return None


def completion_blockers(
    root: Path, state: dict[str, Any], events: list[dict[str, Any]]
) -> list[str]:
    blockers: list[str] = []
    if state["status"] == "blocked":
        reference = latest_block_reference(events)
        suffix = f" ({reference})" if reference else ""
        blockers.append(f"mission is blocked; run resume before completing{suffix}")
    required_artifacts = [item for item in state["artifacts"] if item["required"]]
    required_checks = [item for item in state["verification"] if item["required"]]
    if not required_artifacts and not required_checks:
        blockers.append("mission has no enforceable artifact or verification gate")

    drifted = artifact_drift(root, state)
    for artifact in required_artifacts:
        if artifact["hash"] is None:
            blockers.append(f"required artifact was not recorded: {artifact['path']}")
        elif artifact["path"] in drifted:
            blockers.append(f"required artifact is missing or drifted: {artifact['path']}")

    snapshot = artifact_snapshot_hash(state)
    for index, check in enumerate(state["verification"]):
        if check["required"] and check["status"] != "passed":
            blockers.append(f"verification {index} is {check['status']}")
        if check["status"] == "passed":
            if not check["evidence"]:
                blockers.append(f"verification {index} has no evidence")
            else:
                try:
                    validate_evidence_reference(root, check["evidence"], snapshot)
                except MissionError as error:
                    blockers.append(f"verification {index} evidence invalid: {error}")

    satisfied = satisfied_criteria(state, events)
    for index, criterion in enumerate(state["completion_contract"]):
        if index not in satisfied:
            blockers.append(f"completion criterion {index} is unsatisfied: {criterion}")
    for question in state["open_questions"]:
        blockers.append(f"open question: {question}")
    for dependency in state["dependencies"]:
        try:
            dependency_state = load_consistent_dependency(root, dependency)
        except MissionError:
            blockers.append(f"missing or inconsistent dependency: {dependency}")
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
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        state, baseline_hash, events = load_locked_state(root_path, mission_id, paths)
        if state["status"] in {"complete", "cancelled", "verifying"}:
            raise MissionError(f"cannot complete mission in status {state['status']}")
        blockers = completion_blockers(root_path, state, events)
        if state["status"] == "blocked":
            return state, blockers
        state["updated_at"] = utc_now()
        if blockers:
            state["status"] = "blocked"
            commit_locked(
                paths,
                state,
                "mission_blocked",
                actor,
                {"blocker_count": len(blockers), "blockers_hash": hash_value(blockers)},
                baseline_hash,
            )
            return state, blockers
        state["status"] = "complete"
        commit_locked(
            paths,
            state,
            "mission_completed",
            actor,
            {"completion_criteria_count": len(state["completion_contract"])},
            baseline_hash,
        )
        return state, []


def block_mission(
    root: str | Path, mission_id: str, reason: str, actor: str
) -> dict[str, Any]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        state, baseline_hash, _ = load_locked_state(root_path, mission_id, paths)
        reject_mutation_status(state, "block")
        state["status"] = "blocked"
        state["updated_at"] = utc_now()
        commit_locked(
            paths,
            state,
            "mission_blocked",
            actor,
            {"reason_hash": hash_value(reason)},
            baseline_hash,
        )
        return state


def resume_mission(root: str | Path, mission_id: str, actor: str) -> dict[str, Any]:
    root_path = repo_root(root)
    paths = mission_paths(root_path, mission_id)
    with exclusive_lock(paths["lock"]):
        state, baseline_hash, _ = load_locked_state(root_path, mission_id, paths)
        if state["status"] != "blocked":
            raise MissionError("only blocked missions can be resumed")
        state["status"] = "active"
        state["updated_at"] = utc_now()
        commit_locked(paths, state, "mission_resumed", actor, {}, baseline_hash)
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
        state, baseline_hash, events = load_locked_state(root_path, mission_id, paths)
        if state["status"] == "verifying":
            raise MissionError("cannot audit a mission while verification is running")
        snapshot = artifact_snapshot_hash(state)
        evidence_count = 0
        for check in state["verification"]:
            if check["evidence"]:
                validate_evidence_reference(root_path, check["evidence"], snapshot)
                evidence_count += 1
        drifted = artifact_drift(root_path, state) if deep else []
        if drifted:
            raise MissionError(f"mission artifact drift detected: {drifted}")
        event = commit_locked(
            paths,
            state,
            "mission_audited",
            actor,
            {
                "deep": deep,
                "event_count_before_audit": len(events),
                "evidence_count": evidence_count,
            },
            baseline_hash,
        )
        return {
            "mission_id": mission_id,
            "status": state["status"],
            "event_count": len(events) + 1,
            "audit_event_hash": event["event_hash"],
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
        "completion_criteria": len(state["completion_contract"]),
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

    criterion = commands.add_parser("satisfy-criterion")
    criterion.add_argument("mission_id")
    criterion.add_argument("--index", type=int, required=True)
    criterion.add_argument("--evidence", required=True)

    question = commands.add_parser("resolve-question")
    question.add_argument("mission_id")
    question.add_argument("--index", type=int, required=True)
    question.add_argument("--answer", required=True)

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
        elif args.command == "satisfy-criterion":
            output = summary(
                satisfy_criterion(
                    args.root,
                    args.mission_id,
                    args.index,
                    args.evidence,
                    args.actor,
                )
            )
        elif args.command == "resolve-question":
            output = summary(
                resolve_question(
                    args.root,
                    args.mission_id,
                    args.index,
                    args.answer,
                    args.actor,
                )
            )
        elif args.command == "complete":
            state, blockers = complete_mission(args.root, args.mission_id, args.actor)
            output = {"mission": summary(state), "blockers": blockers}
        elif args.command == "block":
            output = summary(
                block_mission(args.root, args.mission_id, args.reason, args.actor)
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
    if isinstance(output, dict) and output.get("missing_required"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
