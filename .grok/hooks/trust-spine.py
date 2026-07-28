#!/usr/bin/env python3
"""Emit a privacy-preserving, hash-chained GlacierEQ action receipt."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

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


def last_hash(handle: Any) -> str | None:
    handle.seek(0)
    previous: str | None = None
    for line in handle:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = item.get("receipt_hash")
        if isinstance(value, str) and len(value) == 64:
            previous = value
    return previous


def main() -> int:
    event = load_event()
    path = receipt_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)

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
    payload_hash = sha256(safe_event)

    with path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass

        parent = last_hash(handle)
        core = {
            "schema_version": 1,
            "receipt_id": "grx_"
            + hashlib.sha256(
                f"{event.get('sessionId', '')}:{timestamp}:{payload_hash}".encode("utf-8")
            ).hexdigest()[:24],
            "event_name": event_name,
            "timestamp": timestamp,
            "session_id": str(event.get("sessionId") or ""),
            "workspace": str(event.get("workspaceRoot") or event.get("cwd") or ""),
            "permission_mode": event.get("permissionMode"),
            "tool_name": event.get("toolName"),
            "status": event.get("status")
            or event.get("outcome")
            or event.get("errorType"),
            "payload_hash": payload_hash,
            "parent_hash": parent,
            "metadata": {
                "tool_use_id": event.get("toolUseId"),
                "input_truncated": bool(event.get("toolInputTruncated", False)),
                "subagent_type": event.get("subagentType"),
                "source": "grok-project-hook",
            },
        }
        core["receipt_hash"] = sha256(core)
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(core, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"trust-spine hook failed: {error}", file=sys.stderr)
        raise SystemExit(1)
