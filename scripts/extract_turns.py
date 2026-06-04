#!/usr/bin/env python3
"""Extract User/Copilot turns from a Copilot session .jsonl log.

Supports two formats:

* **VS Code GitHub Copilot Chat** — event-sourced with ``kind=0`` initial
  snapshot plus ``kind=1`` (scalar) / ``kind=2`` (structured) patches against
  a ``requests`` array tree.
* **GitHub Copilot CLI** — flat event stream where each line is a record with
  ``type`` (e.g. ``user.message``, ``assistant.message``), ``data``, ``id``,
  ``timestamp``, ``parentId``.

The output is a JSON array of turns in temporal order. Each turn is either::

    {"role": "User", "text": ..., "timestamp": "..."}
    {"role": "Copilot", "parts": [{"type": "response_text"|"tool"|..., "text": ...}, ...], "timestamp": "..."}

``timestamp`` is an ISO-8601 UTC string when known and omitted otherwise.

Usage:
    python extract_turns.py <session.jsonl> [--out transcript.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _apply_patch(requests: list[dict], path: list[Any], value: Any) -> None:
    """Apply a single keyed update to the requests tree at the given path."""
    if not path or path[0] != "requests":
        return
    if len(path) == 1:
        if isinstance(value, list):
            requests.extend(value)
        return
    idx = path[1]
    while len(requests) <= idx:
        requests.append({})
    target: Any = requests[idx]
    for p in path[2:-1]:
        if isinstance(target, list):
            while len(target) <= p:
                target.append({})
            target = target[p]
        else:
            target = target.setdefault(p, {})
    last = path[-1]
    if isinstance(target, list):
        while len(target) <= last:
            target.append(None)
        target[last] = value
    else:
        target[last] = value


def load_requests(path: Path) -> list[dict]:
    """Reconstruct the final `requests` array from the jsonl event stream."""
    requests: list[dict] = []
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            k = obj.get("kind")
            if k == 0:
                requests.extend(obj["v"].get("requests", []) or [])
            elif k in (1, 2):
                _apply_patch(requests, obj.get("k") or [], obj.get("v"))
    return requests


def _user_text(request: dict) -> str:
    msg = request.get("message")
    if isinstance(msg, dict):
        return (msg.get("text") or "").strip()
    return (msg or "").strip() if isinstance(msg, str) else ""


def _format_tool(item: dict) -> str:
    msg = item.get("pastTenseMessage") or item.get("invocationMessage") or {}
    text = msg.get("value") if isinstance(msg, dict) else str(msg)
    return text or ""


def _copilot_parts(request: dict, include_tools: bool = True) -> list[dict]:
    """Split a Copilot response into ordered {type, text} sub-parts.

    Consecutive `response_text` parts are concatenated into a single part so
    that adjacent markdown chunks read as one block.
    """
    parts: list[dict] = []

    def _append(kind: str, text: str) -> None:
        if not text:
            return
        if kind == "response_text" and parts and parts[-1]["type"] == "response_text":
            parts[-1]["text"] += text
        else:
            parts.append({"type": kind, "text": text})

    for item in request.get("response") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind is None:
            val = item.get("value")
            if isinstance(val, str) and val.strip():
                _append("response_text", val)
        elif kind == "toolInvocationSerialized" and include_tools:
            _append("tool", _format_tool(item))
        elif kind == "inlineReference":
            _append("inline_reference", item.get("name") or "")
        # thinking / undoStop / codeblockUri / textEditGroup / mcpServersStarting
        # / questionCarousel are skipped — they are control or opaque payloads.
    return parts


def build_turns(requests: list[dict], include_tools: bool = True) -> list[dict]:
    """Flatten requests into User turns + Copilot turns with sub-parts."""
    turns: list[dict] = []
    for r in requests:
        ts = _epoch_ms_to_iso(r.get("timestamp"))
        user = _user_text(r)
        if user:
            turn: dict[str, Any] = {"role": "User", "text": user}
            if ts:
                turn["timestamp"] = ts
            turns.append(turn)
        parts = _copilot_parts(r, include_tools=include_tools)
        if parts:
            turn = {"role": "Copilot", "parts": parts}
            if ts:
                turn["timestamp"] = ts
            turns.append(turn)
    return turns


# ---------------------------------------------------------------------------
# Copilot CLI (`type`/`data` event stream) format
# ---------------------------------------------------------------------------


def _epoch_ms_to_iso(value: Any) -> str | None:
    """Normalize a millisecond-precision epoch int into ISO-8601 (UTC)."""
    if not isinstance(value, (int, float)):
        return None
    try:
        return (
            datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _is_system_reminder(content: str) -> bool:
    """Detect a Copilot CLI auto-injected ``<system_reminder>`` user message."""
    if not isinstance(content, str):
        return False
    return content.lstrip().startswith("<system_reminder>")


_CLI_TOOL_ARG_KEYS: dict[str, str] = {
    # tool name -> argument key whose value summarizes the call
    "bash": "command",
    "view": "path",
    "edit": "path",
    "create": "path",
    "glob": "pattern",
    "grep": "pattern",
    "rg": "pattern",
    "web_fetch": "url",
    "web_search": "query",
    "ask_user": "question",
}


def _format_tool_cli(tool_request: dict) -> str:
    """Produce a short human-readable summary of a single CLI tool call."""
    if not isinstance(tool_request, dict):
        return ""
    summary = tool_request.get("intentionSummary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    name = tool_request.get("name") or "tool"
    args = tool_request.get("arguments") or {}
    if not isinstance(args, dict):
        return name
    if name == "report_intent":
        intent = args.get("intent")
        if isinstance(intent, str) and intent.strip():
            return intent.strip()
    key = _CLI_TOOL_ARG_KEYS.get(name)
    if key and isinstance(args.get(key), str) and args[key].strip():
        return f"{name}: {args[key].strip()}"
    return name


def _new_copilot_turn() -> dict[str, Any]:
    return {"role": "Copilot", "parts": []}


def _append_part(parts: list[dict], kind: str, text: str) -> None:
    if not text:
        return
    if kind == "response_text" and parts and parts[-1]["type"] == "response_text":
        parts[-1]["text"] += text
    else:
        parts.append({"type": kind, "text": text})


def _flush_copilot(turns: list[dict], pending: dict[str, Any] | None) -> None:
    if pending and pending["parts"]:
        turns.append(pending)


def build_turns_cli(events: Iterator[dict], include_tools: bool = True) -> list[dict]:
    """Flatten a CLI event stream into the same User/Copilot turn schema."""
    turns: list[dict] = []
    pending: dict[str, Any] | None = None

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        data = event.get("data") or {}
        ts = event.get("timestamp")

        if etype == "user.message":
            content = data.get("content") if isinstance(data, dict) else ""
            if not isinstance(content, str) or not content.strip():
                continue
            if _is_system_reminder(content):
                continue
            _flush_copilot(turns, pending)
            pending = None
            turn: dict[str, Any] = {"role": "User", "text": content.strip()}
            if isinstance(ts, str) and ts:
                turn["timestamp"] = ts
            turns.append(turn)

        elif etype == "assistant.message":
            if pending is None:
                pending = _new_copilot_turn()
                if isinstance(ts, str) and ts:
                    pending["timestamp"] = ts
            content = data.get("content") if isinstance(data, dict) else None
            if isinstance(content, str) and content.strip():
                _append_part(pending["parts"], "response_text", content)
            if include_tools:
                for tr in data.get("toolRequests") or []:
                    _append_part(pending["parts"], "tool", _format_tool_cli(tr))

        elif etype == "abort":
            # Aborts end a turn early; flush whatever the assistant produced so
            # the partial response is preserved in the transcript.
            _flush_copilot(turns, pending)
            pending = None

        # Everything else (tool.execution_*, hook.*, permission.*, system.*,
        # session.*, assistant.turn_*) is intentionally ignored.

    _flush_copilot(turns, pending)
    return turns


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _peek_first(path: Path) -> dict:
    for event in _iter_jsonl(path):
        return event
    raise ValueError(f"{path} is empty")


def detect_format(path: Path) -> str:
    """Return ``'vscode'`` or ``'cli'`` based on the first event's shape."""
    first = _peek_first(path)
    has_kind = "kind" in first and isinstance(first.get("kind"), int)
    has_type = "type" in first and isinstance(first.get("type"), str)
    if has_kind and has_type:
        raise ValueError(
            f"{path}: first event has both 'kind' and 'type' fields; "
            "cannot determine format"
        )
    if has_kind:
        if first["kind"] not in (0, 1, 2):
            raise ValueError(
                f"{path}: first event has unexpected kind={first['kind']!r}"
            )
        return "vscode"
    if has_type:
        return "cli"
    raise ValueError(
        f"{path}: first event has neither 'kind' nor 'type'; unknown format"
    )


def extract_turns(path: Path, include_tools: bool = True) -> list[dict]:
    """Dispatch to the appropriate loader based on detected file format."""
    fmt = detect_format(path)
    if fmt == "vscode":
        return build_turns(load_requests(path), include_tools=include_tools)
    if fmt == "cli":
        return build_turns_cli(_iter_jsonl(path), include_tools=include_tools)
    raise ValueError(f"unknown format: {fmt}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", type=Path, help="Path to a .jsonl session file")
    ap.add_argument("--out", type=Path, default=None,
                    help="Write JSON to this file (default: stdout)")
    ap.add_argument("--no-tools", action="store_true",
                    help="Omit tool-invocation lines from Copilot turns")
    args = ap.parse_args()

    if not args.session.exists():
        print(f"error: {args.session} not found", file=sys.stderr)
        return 1

    try:
        turns = extract_turns(args.session, include_tools=not args.no_tools)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(turns, indent=2, ensure_ascii=False)

    if args.out:
        args.out.write_text(payload + "\n", encoding="utf-8")
        print(f"Wrote {len(turns)} turn(s) to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
