#!/usr/bin/env python3
"""Emit privacy-preserving, hash-chained GlacierEQ action receipts."""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
from typing import Any, Iterator

SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)
RAW_PAYLOAD_KEYS = {
    "prompt",
    "toolInput",
    "toolOutput",
    "toolResponse",
    "response",
    "content",
}
MAX_SCALAR_LENGTH = 256
LOCK_TIMEOUT_SECS = 5.0
STALE_LOCK_SECS = 60.0
HEAD_SCHEMA_VERSION = 1


class ChainIntegrityError(RuntimeError):
    """Raised when receipt state cannot be verified safely."""


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def scrub(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(part in lowered for part in SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if key in RAW_PAYLOAD_KEYS:
        return {"sha256": sha256(value), "stored": False}
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value[:64]]
    if isinstance(value, str):
        return value if len(value) <= MAX_SCALAR_LENGTH else value[:MAX_SCALAR_LENGTH] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_SCALAR_LENGTH]


def optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_event() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("hook payload exceeds 4 MiB")
    parsed = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("hook payload must be an object")
    return parsed


def receipt_path(event: dict[str, Any]) -> Path:
    root_text = event.get("workspaceRoot") or event.get("cwd") or os.getcwd()
    root = Path(str(root_text)).expanduser().resolve()
    return root / ".grok" / "runtime" / "trust-spine" / "receipts.jsonl"


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Acquire a cross-platform lock using atomic exclusive file creation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{secrets.token_hex(16)}"
    deadline = time.monotonic() + LOCK_TIMEOUT_SECS
    descriptor: int | None = None

    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, token.encode("utf-8"))
            os.fsync(descriptor)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > STALE_LOCK_SECS:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring receipt lock: {path}")
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


def verify_receipt(item: Any, expected_parent: str | None = None) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ChainIntegrityError("receipt is not an object")
    if expected_parent is not None and item.get("parent_hash") != expected_parent:
        raise ChainIntegrityError("receipt parent hash mismatch")
    stored_hash = item.get("receipt_hash")
    if not isinstance(stored_hash, str) or len(stored_hash) != 64:
        raise ChainIntegrityError("invalid receipt hash")
    core = dict(item)
    core.pop("receipt_hash", None)
    if sha256(core) != stored_hash:
        raise ChainIntegrityError("receipt hash mismatch")
    receipt_id = item.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id.startswith("grx_"):
        raise ChainIntegrityError("invalid receipt id")
    return item


def verify_chain(handle: Any) -> tuple[str | None, str | None, int]:
    """Perform a full chain verification for recovery and explicit auditing."""

    handle.seek(0)
    previous: str | None = None
    last_id: str | None = None
    count = 0
    seen_ids: set[str] = set()
    for line_number, line in enumerate(handle, start=1):
        if not line.endswith("\n"):
            raise ChainIntegrityError(f"partial receipt line at {line_number}")
        if not line.strip():
            raise ChainIntegrityError(f"blank receipt line at {line_number}")
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise ChainIntegrityError(
                f"malformed receipt JSON at line {line_number}"
            ) from error
        if item.get("parent_hash") != previous:
            raise ChainIntegrityError(f"parent hash mismatch at line {line_number}")
        verify_receipt(item)
        receipt_id = item["receipt_id"]
        if receipt_id in seen_ids:
            raise ChainIntegrityError(f"duplicate receipt id at line {line_number}")
        seen_ids.add(receipt_id)
        previous = item["receipt_hash"]
        last_id = receipt_id
        count += 1
    return previous, last_id, count


def read_last_receipt(path: Path) -> dict[str, Any] | None:
    """Read only the final complete JSONL record."""

    size = path.stat().st_size
    if size == 0:
        return None
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) != b"\n":
            raise ChainIntegrityError("receipt log ends with a partial line")
        position = size - 2
        while position >= 0:
            handle.seek(position)
            if handle.read(1) == b"\n":
                position += 1
                break
            position -= 1
        if position < 0:
            position = 0
        handle.seek(position)
        raw = handle.read(size - position).rstrip(b"\n")
    try:
        return verify_receipt(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChainIntegrityError("malformed final receipt") from error


def head_hash(state: dict[str, Any]) -> str:
    core = dict(state)
    core.pop("state_hash", None)
    return sha256(core)


def load_head(path: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict) or state.get("schema_version") != HEAD_SCHEMA_VERSION:
        return None
    if state.get("state_hash") != head_hash(state):
        return None
    return state


def write_head(path: Path, state: dict[str, Any]) -> None:
    state = dict(state)
    state["state_hash"] = head_hash(state)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def recover_head(handle: Any, log_path: Path, head_path: Path) -> dict[str, Any]:
    final_hash, receipt_id, count = verify_chain(handle)
    state = {
        "schema_version": HEAD_SCHEMA_VERSION,
        "log_size": log_path.stat().st_size,
        "receipt_count": count,
        "head_hash": final_hash,
        "receipt_id": receipt_id,
        "updated_at": utc_now(),
    }
    write_head(head_path, state)
    return state


def current_head(handle: Any, log_path: Path, head_path: Path) -> dict[str, Any]:
    """Validate the compact head and tail; full-scan only for recovery."""

    size = log_path.stat().st_size
    state = load_head(head_path)
    if state is None or state.get("log_size") != size:
        return recover_head(handle, log_path, head_path)
    if size == 0:
        if state.get("head_hash") is not None or state.get("receipt_count") != 0:
            return recover_head(handle, log_path, head_path)
        return state
    tail = read_last_receipt(log_path)
    if tail is None:
        return recover_head(handle, log_path, head_path)
    if tail.get("receipt_hash") != state.get("head_hash"):
        return recover_head(handle, log_path, head_path)
    if tail.get("receipt_id") != state.get("receipt_id"):
        return recover_head(handle, log_path, head_path)
    return state


def build_receipt(event: dict[str, Any], parent_hash: str | None) -> dict[str, Any]:
    safe_event = scrub(event)
    event_name = str(
        event.get("hookEventName")
        or event.get("eventName")
        or event.get("event")
        or "unknown"
    )
    timestamp = str(event.get("timestamp") or utc_now())
    status = event.get("status") or event.get("outcome") or event.get("errorType")
    core: dict[str, Any] = {
        "schema_version": 1,
        "receipt_id": "grx_" + secrets.token_hex(12),
        "event_name": event_name,
        "timestamp": timestamp,
        "session_id": str(event.get("sessionId") or ""),
        "workspace": str(event.get("workspaceRoot") or event.get("cwd") or ""),
        "permission_mode": optional_string(event.get("permissionMode")),
        "tool_name": optional_string(event.get("toolName")),
        "status": optional_string(status),
        "payload_hash": sha256(safe_event),
        "parent_hash": parent_hash,
        "metadata": {
            "tool_use_id": optional_string(event.get("toolUseId")),
            "input_truncated": bool(event.get("toolInputTruncated", False)),
            "result_truncated": bool(
                event.get("toolResultTruncated", event.get("toolOutputTruncated", False))
            ),
            "subagent_id": optional_string(event.get("subagentId")),
            "subagent_type": optional_string(event.get("subagentType")),
            "source": "grok-project-hook",
        },
    }
    core["receipt_hash"] = sha256(core)
    return core


def emit_receipt(event: dict[str, Any]) -> dict[str, Any]:
    """Append one receipt after constant-time head/tail validation."""

    path = receipt_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    head_path = path.with_suffix(path.suffix + ".head.json")

    with exclusive_lock(lock_path):
        with path.open("a+", encoding="utf-8") as handle:
            state = current_head(handle, path, head_path)
            receipt = build_receipt(event, state.get("head_hash"))
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            next_state = {
                "schema_version": HEAD_SCHEMA_VERSION,
                "log_size": path.stat().st_size,
                "receipt_count": int(state.get("receipt_count", 0)) + 1,
                "head_hash": receipt["receipt_hash"],
                "receipt_id": receipt["receipt_id"],
                "updated_at": utc_now(),
            }
            write_head(head_path, next_state)
    return receipt


def main() -> int:
    emit_receipt(load_event())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"trust-spine hook failed: {error}", file=sys.stderr)
        raise SystemExit(1)
