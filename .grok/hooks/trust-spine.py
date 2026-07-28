#!/usr/bin/env python3
"""Emit a privacy-preserving, hash-chained GlacierEQ action receipt."""

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


class ChainIntegrityError(RuntimeError):
    """Raised when an existing receipt log cannot be verified end to end."""


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
                age = time.time() - path.stat().st_mtime
                if age > STALE_LOCK_SECS:
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


def verify_chain(handle: Any) -> str | None:
    """Verify every existing record and return the final receipt hash."""

    handle.seek(0)
    previous: str | None = None
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
        if not isinstance(item, dict):
            raise ChainIntegrityError(f"non-object receipt at line {line_number}")
        if item.get("parent_hash") != previous:
            raise ChainIntegrityError(f"parent hash mismatch at line {line_number}")
        stored_hash = item.get("receipt_hash")
        if not isinstance(stored_hash, str) or len(stored_hash) != 64:
            raise ChainIntegrityError(f"invalid receipt hash at line {line_number}")
        core = dict(item)
        core.pop("receipt_hash", None)
        if sha256(core) != stored_hash:
            raise ChainIntegrityError(f"receipt hash mismatch at line {line_number}")
        receipt_id = item.get("receipt_id")
        if not isinstance(receipt_id, str) or receipt_id in seen_ids:
            raise ChainIntegrityError(f"duplicate or invalid receipt id at line {line_number}")
        seen_ids.add(receipt_id)
        previous = stored_hash
    return previous


def build_receipt(event: dict[str, Any], parent_hash: str | None) -> dict[str, Any]:
    safe_event = scrub(event)
    event_name = str(
        event.get("hookEventName")
        or event.get("eventName")
        or event.get("event")
        or "unknown"
    )
    timestamp = str(
        event.get("timestamp")
        or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )
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
            "subagent_type": optional_string(event.get("subagentType")),
            "source": "grok-project-hook",
        },
    }
    core["receipt_hash"] = sha256(core)
    return core


def emit_receipt(event: dict[str, Any]) -> dict[str, Any]:
    """Verify the existing chain, append one receipt, and return it."""

    path = receipt_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    with exclusive_lock(lock_path):
        with path.open("a+", encoding="utf-8") as handle:
            parent = verify_chain(handle)
            receipt = build_receipt(event, parent)
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
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
