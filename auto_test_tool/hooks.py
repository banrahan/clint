"""Pre/post host shell hooks for scenario YAMLs.

A scenario can declare optional `pre:` and `post:` lists of shell hooks that
run on the host (not inside the tmux session). Each entry is either:

    - "mkdir -p /tmp/foo"          # string shorthand

or a mapping:

    - run: "git init"
      cwd: "/tmp/foo"              # optional
      env:                         # optional, merged onto os.environ
        FOO: "bar"
      continue_on_error: true      # optional, default false
      timeout: 60                  # optional seconds, default 120
      name: "init git repo"        # optional label

Execution is sequential. The first failing hook aborts the run unless that
hook opts in to `continue_on_error: true`; aborted runs mark the remaining
hooks as `skipped` so the report stays complete.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT_S = 120
_STDIO_TAIL_CHARS = 2000


@dataclass
class Hook:
    run: str
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    continue_on_error: bool = False
    timeout: int = DEFAULT_TIMEOUT_S
    name: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.run


@dataclass
class HookResult:
    hook: Hook
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    skipped: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        if self.skipped:
            return False
        return self.exit_code == 0 and not self.timed_out


def parse_hooks(raw: Any, scenario_cwd: str | None = None) -> list[Hook]:
    """Parse a raw YAML list into Hook objects.

    Accepts either strings or mappings. Raises ValueError on malformed entries.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Expected a list of hooks, got {type(raw).__name__}")

    hooks: list[Hook] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            hooks.append(Hook(run=entry, cwd=scenario_cwd))
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"Hook {i}: expected string or mapping, got {type(entry).__name__}")

        run = entry.get("run")
        if not run or not isinstance(run, str):
            raise ValueError(f"Hook {i}: missing required string field 'run'")

        env_raw = entry.get("env") or {}
        if not isinstance(env_raw, dict):
            raise ValueError(f"Hook {i}: 'env' must be a mapping")
        env = {str(k): str(v) for k, v in env_raw.items()}

        timeout = entry.get("timeout", DEFAULT_TIMEOUT_S)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"Hook {i}: 'timeout' must be a positive number")

        hooks.append(
            Hook(
                run=run,
                cwd=entry.get("cwd") or scenario_cwd,
                env=env,
                continue_on_error=bool(entry.get("continue_on_error", False)),
                timeout=int(timeout),
                name=entry.get("name"),
            )
        )
    return hooks


def execute_hooks(hooks: list[Hook]) -> list[HookResult]:
    """Run hooks sequentially. Returns a result per hook (including skipped)."""
    results: list[HookResult] = []
    aborted = False
    for hook in hooks:
        if aborted:
            results.append(HookResult(hook=hook, skipped=True))
            continue

        result = _run_one(hook)
        results.append(result)
        if not result.ok and not hook.continue_on_error:
            aborted = True
    return results


def _run_one(hook: Hook) -> HookResult:
    cwd = os.path.expanduser(hook.cwd) if hook.cwd else None
    if cwd:
        os.makedirs(cwd, exist_ok=True)

    full_env = os.environ.copy()
    full_env.update(hook.env)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            hook.run,
            shell=True,
            cwd=cwd,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=hook.timeout,
        )
        duration = time.monotonic() - start
        return HookResult(
            hook=hook,
            exit_code=proc.returncode,
            stdout=_tail(proc.stdout),
            stderr=_tail(proc.stderr),
            duration_s=duration,
        )
    except subprocess.TimeoutExpired as e:
        duration = time.monotonic() - start
        return HookResult(
            hook=hook,
            exit_code=None,
            stdout=_tail(e.stdout or ""),
            stderr=_tail(e.stderr or ""),
            duration_s=duration,
            timed_out=True,
        )


def _tail(text: str | bytes) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) <= _STDIO_TAIL_CHARS:
        return text
    return "...[truncated]...\n" + text[-_STDIO_TAIL_CHARS:]


def format_hook_results(phase: str, results: list[HookResult]) -> str:
    """Render a Copilot-readable summary of hook results."""
    if not results:
        return f"No {phase} hooks to run."

    ok_count = sum(1 for r in results if r.ok)
    failed_count = sum(1 for r in results if not r.ok and not r.skipped)
    skipped_count = sum(1 for r in results if r.skipped)

    lines = [
        f"{phase.capitalize()} hooks: {ok_count} ok, {failed_count} failed, "
        f"{skipped_count} skipped (of {len(results)} total)",
        "",
    ]
    for i, r in enumerate(results, 1):
        if r.skipped:
            status = "SKIPPED"
        elif r.timed_out:
            status = f"TIMEOUT after {r.hook.timeout}s"
        elif r.ok:
            status = f"OK ({r.duration_s:.2f}s)"
        else:
            status = f"FAILED exit={r.exit_code} ({r.duration_s:.2f}s)"
            if r.hook.continue_on_error:
                status += " [continue_on_error]"
        lines.append(f"  {i}. [{status}] {r.hook.label}")
        if r.stdout.strip():
            lines.append(f"     stdout: {_inline(r.stdout)}")
        if r.stderr.strip():
            lines.append(f"     stderr: {_inline(r.stderr)}")
    return "\n".join(lines)


def _inline(text: str) -> str:
    text = text.strip()
    if "\n" not in text and len(text) <= 200:
        return text
    indented = "\n       ".join(text.splitlines())
    return "\n       " + indented
