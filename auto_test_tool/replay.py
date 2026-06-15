"""Deterministic replay runner for recorded test plans.

Reads a ``plans/<name>.plan.yaml`` produced by ``PlanRecorder`` and
re-executes its steps against the same tmux + ``AgentSession`` machinery
used interactively — but with no LLM in the loop.

Usage::

    python -m auto_test_tool.replay plans/smoke-test.plan.yaml
    # or, via the installed console script:
    cli-tester-replay plans/smoke-test.plan.yaml

Exit codes:

* ``0`` — every step's assertions passed.
* ``1`` — at least one step failed (replay is fail-fast — stops at the
  first failure but still runs ``finish_session`` and ``post`` hooks
  so CI artifacts are intact).
* ``2`` — usage / plan-loading error before the session started.

Design notes (matches ``plan.md`` and ``recorder.py``):

* No MCP involvement — replay runs in-process against ``AgentSession``
  directly. One code path for tmux primitives means a bug in the
  picker logic fails CI the same way it fails interactively.
* Fail-fast on first failed assertion, by user choice.
* Always calls ``session.finish()`` and ``execute_hooks(post)`` so the
  HTML report + SVG screenshots are written even on failure.
* ``contains`` assertions poll up to ``timeout_seconds`` so the runner
  is tolerant of CLI latency without baking sleeps into the plan.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import yaml

from .agent import AgentSession
from .hooks import Hook, execute_hooks, format_hook_results, parse_hooks
from .ports import PortPool, parse_allocate_ports
from .runner import tmux_is_installed, tmux_session_alive
from .template import substitute_in_mapping, substitute_template

POLL_INTERVAL_S = 0.5
DEFAULT_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass
class PlanStep:
    index: int
    kind: str  # start | action | observe | screenshot
    action: dict | None = None
    label: str = ""
    contains: list[str] | None = None
    not_contains: list[str] | None = None
    regex: list[str] | None = None
    session_exited: bool | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_S


@dataclass
class Plan:
    name: str
    command: str
    cwd: str | None
    env: dict[str, str]
    allocate_ports: Any
    pre: list[Any]
    post: list[Any]
    steps: list[PlanStep]
    source_scenario: str | None = None
    schema_version: int = 1


def load_plan(path: str) -> Plan:
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise FileNotFoundError(f"Plan file not found: {expanded}")
    with open(expanded) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Plan root must be a mapping, got {type(data).__name__}")

    schema_version = data.get("schema_version", 1)
    if schema_version != 1:
        raise ValueError(
            f"Unsupported plan schema_version {schema_version!r}; this runner only handles v1."
        )

    raw_steps = data.get("steps") or []
    if not isinstance(raw_steps, list):
        raise ValueError("`steps` must be a list")
    steps: list[PlanStep] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"step[{i}] must be a mapping, got {type(raw).__name__}")
        kind = raw.get("kind")
        if kind not in ("start", "action", "observe", "screenshot"):
            raise ValueError(
                f"step[{i}].kind must be one of start/action/observe/screenshot, got {kind!r}"
            )
        assert_block = raw.get("assert") or {}
        if not isinstance(assert_block, dict):
            raise ValueError(f"step[{i}].assert must be a mapping")
        steps.append(
            PlanStep(
                index=raw.get("index", i),
                kind=kind,
                action=raw.get("action"),
                label=raw.get("label", ""),
                contains=list(assert_block.get("contains") or []) or None,
                not_contains=list(assert_block.get("not_contains") or []) or None,
                regex=list(assert_block.get("regex") or []) or None,
                session_exited=assert_block.get("session_exited"),
                timeout_seconds=float(assert_block.get("timeout_seconds", DEFAULT_TIMEOUT_S)),
            )
        )

    return Plan(
        schema_version=schema_version,
        name=data.get("name") or "unnamed-plan",
        source_scenario=data.get("source_scenario"),
        command=data["command"],
        cwd=data.get("cwd"),
        env=dict(data.get("env") or {}),
        allocate_ports=data.get("allocate_ports"),
        pre=list(data.get("pre") or []),
        post=list(data.get("post") or []),
        steps=steps,
    )


# ---------------------------------------------------------------------------
# Assertion evaluation
# ---------------------------------------------------------------------------


class AssertionFailure(Exception):
    pass


def _wait_for_contains(
    session: AgentSession,
    contains: list[str] | None,
    timeout_s: float,
    initial_capture: str,
) -> str:
    """Poll ``observe()`` until every contains pattern appears, or timeout.

    Returns the most recent capture. Raises ``AssertionFailure`` on timeout.
    """
    if not contains:
        return initial_capture
    deadline = time.time() + max(timeout_s, 0.0)
    capture = initial_capture
    missing: list[str] = []
    while True:
        missing = [needle for needle in contains if needle not in capture]
        if not missing:
            return capture
        if time.time() >= deadline:
            raise AssertionFailure(
                f"contains assertion timed out after {timeout_s:.1f}s; "
                f"missing: {missing!r}\n--- last capture (tail) ---\n{_tail(capture)}"
            )
        time.sleep(POLL_INTERVAL_S)
        capture = session.observe()


def _check_not_contains(capture: str, not_contains: list[str] | None) -> None:
    if not not_contains:
        return
    hits = [needle for needle in not_contains if needle in capture]
    if hits:
        raise AssertionFailure(
            f"not_contains assertion failed; forbidden text appeared: {hits!r}\n"
            f"--- last capture (tail) ---\n{_tail(capture)}"
        )


def _check_regex(capture: str, regex: list[str] | None) -> None:
    if not regex:
        return
    missed: list[str] = []
    for pattern in regex:
        try:
            if not re.search(pattern, capture):
                missed.append(pattern)
        except re.error as e:
            raise AssertionFailure(f"invalid regex {pattern!r}: {e}")
    if missed:
        raise AssertionFailure(
            f"regex assertion failed; no match for: {missed!r}\n"
            f"--- last capture (tail) ---\n{_tail(capture)}"
        )


def _check_session_exited(
    session: AgentSession,
    expected: bool | None,
) -> None:
    if expected is None:
        return
    alive = tmux_session_alive(session.session_name)
    actual_exited = not alive
    if actual_exited != expected:
        raise AssertionFailure(
            f"session_exited assertion failed: expected {expected}, "
            f"observed exited={actual_exited} (alive={alive})"
        )


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------------------
# Replay driver
# ---------------------------------------------------------------------------


@dataclass
class StepOutcome:
    index: int
    kind: str
    success: bool
    error: str = ""
    elapsed_seconds: float = 0.0


@dataclass
class ReplayResult:
    plan_name: str
    success: bool
    outcomes: list[StepOutcome]
    report_path: str = ""
    pre_summary: str = ""
    post_summary: str = ""


def _resolve_plan_strings(plan: Plan, pool: PortPool) -> Plan:
    """Apply ``{name}`` substitution to command/cwd/env using pool vars.

    Plans are usually written with literal values (the recorder
    substitutes before writing), but allow hand-edited plans to use
    placeholders for parity with scenarios.
    """
    vars_dict: dict[str, object] = dict(pool.vars()) if pool.names else {}
    # Expose {cwd} the same way the MCP server does.
    if plan.cwd:
        try:
            vars_dict.setdefault("cwd", substitute_template(plan.cwd, vars_dict))
        except KeyError:
            pass
    try:
        resolved_cwd = substitute_template(plan.cwd, vars_dict) if plan.cwd else plan.cwd
        resolved_command = substitute_template(plan.command, vars_dict)
        resolved_env = substitute_in_mapping(plan.env or {}, vars_dict)
    except KeyError as e:
        raise ValueError(f"Unresolved placeholder in plan: {e}")
    return Plan(
        schema_version=plan.schema_version,
        name=plan.name,
        source_scenario=plan.source_scenario,
        command=resolved_command,
        cwd=resolved_cwd,
        env=resolved_env,
        allocate_ports=plan.allocate_ports,
        pre=plan.pre,
        post=plan.post,
        steps=plan.steps,
    )


def replay(
    plan: Plan,
    output_dir: str = "reports",
    run_name: str | None = None,
    on_step: "Callable[[StepOutcome], None] | None" = None,
) -> ReplayResult:
    """Execute a plan against AgentSession. Returns structured outcome.

    Args:
        plan: The parsed plan to execute.
        output_dir: Where to write the HTML report + screenshots.
        run_name: Name of the sub-folder under ``output_dir``.
        on_step: Optional callback fired after each step completes with
            its :class:`StepOutcome`. Used by the CLI to stream PASS/FAIL
            lines as the replay runs rather than dumping them all at end.
    """
    pool = parse_allocate_ports(plan.allocate_ports)
    plan = _resolve_plan_strings(plan, pool)

    pre_summary = ""
    post_summary = ""

    # --- pre hooks ------------------------------------------------------
    if plan.pre:
        pre_hooks: list[Hook] = parse_hooks(plan.pre, scenario_cwd=plan.cwd)
        results = execute_hooks(pre_hooks)
        pre_summary = format_hook_results("pre", results)
        if any(not r.ok and not r.skipped for r in results):
            return ReplayResult(
                plan_name=plan.name,
                success=False,
                outcomes=[
                    StepOutcome(index=-1, kind="pre", success=False, error="pre-hook failed")
                ],
                pre_summary=pre_summary,
            )

    if run_name is None:
        run_name = f"replay_{plan.name}_{datetime.now():%Y%m%d_%H%M%S}"

    session = AgentSession(
        command=plan.command,
        cwd=plan.cwd,
        env=plan.env,
        output_dir=output_dir,
        run_name=run_name,
    )

    outcomes: list[StepOutcome] = []
    overall_success = True

    initial_capture = session.start()
    current_capture = initial_capture

    try:
        for step in plan.steps:
            start = time.time()
            try:
                current_capture = _execute_step(session, step, current_capture)
                outcome = StepOutcome(
                    index=step.index,
                    kind=step.kind,
                    success=True,
                    elapsed_seconds=time.time() - start,
                )
                outcomes.append(outcome)
                if on_step is not None:
                    on_step(outcome)
            except AssertionFailure as e:
                outcome = StepOutcome(
                    index=step.index,
                    kind=step.kind,
                    success=False,
                    error=str(e),
                    elapsed_seconds=time.time() - start,
                )
                outcomes.append(outcome)
                overall_success = False
                # Capture the failing state so CI artifacts show what we saw.
                try:
                    session.screenshot(label=f"FAIL step {step.index} ({step.kind})")
                except Exception:
                    pass
                if on_step is not None:
                    on_step(outcome)
                break  # fail-fast
            except Exception as e:
                outcome = StepOutcome(
                    index=step.index,
                    kind=step.kind,
                    success=False,
                    error=f"unexpected error: {e}",
                    elapsed_seconds=time.time() - start,
                )
                outcomes.append(outcome)
                overall_success = False
                try:
                    session.screenshot(label=f"ERROR step {step.index}")
                except Exception:
                    pass
                if on_step is not None:
                    on_step(outcome)
                break
    finally:
        session.result.success = overall_success
        report_path = session.finish()

        if plan.post:
            post_hooks: list[Hook] = parse_hooks(plan.post, scenario_cwd=plan.cwd)
            results = execute_hooks(post_hooks)
            post_summary = format_hook_results("post", results)

    return ReplayResult(
        plan_name=plan.name,
        success=overall_success,
        outcomes=outcomes,
        report_path=report_path,
        pre_summary=pre_summary,
        post_summary=post_summary,
    )


def _execute_step(
    session: AgentSession,
    step: PlanStep,
    current_capture: str,
) -> str:
    """Execute a single step. Returns the new capture for assertion chaining."""
    if step.kind == "start":
        # No keystrokes — only assertions on the initial capture.
        capture = _wait_for_contains(
            session, step.contains, step.timeout_seconds, current_capture
        )
        _check_not_contains(capture, step.not_contains)
        _check_regex(capture, step.regex)
        _check_session_exited(session, step.session_exited)
        return capture

    if step.kind == "action":
        if not step.action:
            raise AssertionFailure("step.action is missing for kind='action'")
        capture = session.act(step.action)
        # Surface action-level errors (e.g. LookupError from select_by_text).
        last_step = session.result.steps[-1] if session.result.steps else None
        if last_step and last_step.error:
            raise AssertionFailure(f"action failed: {last_step.error}")
        # Strip the [SESSION EXITED] marker before asserting on text.
        clean = (
            capture[len("[SESSION EXITED]\n"):]
            if capture.startswith("[SESSION EXITED]")
            else capture
        )
        clean = _wait_for_contains(session, step.contains, step.timeout_seconds, clean)
        _check_not_contains(clean, step.not_contains)
        _check_regex(clean, step.regex)
        _check_session_exited(session, step.session_exited)
        return clean

    if step.kind == "observe":
        capture = session.observe()
        clean = (
            capture[len("[SESSION EXITED]\n"):]
            if capture.startswith("[SESSION EXITED]")
            else capture
        )
        clean = _wait_for_contains(session, step.contains, step.timeout_seconds, clean)
        _check_not_contains(clean, step.not_contains)
        _check_regex(clean, step.regex)
        _check_session_exited(session, step.session_exited)
        return clean

    if step.kind == "screenshot":
        session.screenshot(label=step.label)
        return current_capture

    raise AssertionFailure(f"unknown step kind: {step.kind}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _format_header(plan: Plan) -> str:
    lines = [f"Plan: {plan.name}"]
    if plan.source_scenario:
        lines.append(f"Source scenario: {plan.source_scenario}")
    lines.append(f"Steps: {len(plan.steps)}")
    return "\n".join(lines)


def _format_step_line(o: StepOutcome) -> list[str]:
    marker = "PASS" if o.success else "FAIL"
    out = [f"  [{marker}] step {o.index} ({o.kind}) — {o.elapsed_seconds:.2f}s"]
    if not o.success and o.error:
        for err_line in o.error.splitlines():
            out.append(f"        {err_line}")
    return out


def _format_trailer(plan: Plan, result: ReplayResult) -> str:
    lines: list[str] = []
    if result.post_summary:
        lines.append("")
        lines.append(result.post_summary)
    # NB: ``generate_html_report`` already prints "📄 Report: <path>" to
    # stderr inside ``session.finish()``. We deliberately don't re-print
    # the path here so streaming output shows it exactly once.
    lines.append("")
    lines.append(f"Overall: {'PASS' if result.success else 'FAIL'}")
    return "\n".join(lines)


def _format_summary(plan: Plan, result: ReplayResult) -> str:
    """Render the full plan run as a single block.

    Retained for tests and programmatic use. The CLI streams the
    equivalent output incrementally instead of calling this.
    """
    parts = [_format_header(plan)]
    if result.pre_summary:
        parts.append("")
        parts.append(result.pre_summary)
    for o in result.outcomes:
        parts.extend(_format_step_line(o))
    parts.append(_format_trailer(plan, result))
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli-tester-replay",
        description="Replay a recorded test plan against tmux (no LLM in the loop).",
    )
    parser.add_argument("plan", help="Path to plans/<name>.plan.yaml")
    parser.add_argument(
        "--output-dir",
        default="reports",
        help="Where to write screenshots and the HTML report (default: reports)",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="Folder name under output-dir (default: replay_<plan>_<timestamp>)",
    )
    args = parser.parse_args(argv)

    if not tmux_is_installed():
        print("ERROR: tmux is required but not installed.", file=sys.stderr)
        return 2

    try:
        plan = load_plan(args.plan)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # Print plan header before any steps start, so the user sees what's
    # about to run while replay sets up tmux / fires hooks.
    print(_format_header(plan), flush=True)

    def _on_step(o: StepOutcome) -> None:
        for line in _format_step_line(o):
            print(line, flush=True)

    result = replay(
        plan,
        output_dir=args.output_dir,
        run_name=args.run_name,
        on_step=_on_step,
    )
    # Pre-hook summary (if any) gets buffered into result; print it now
    # in front of the trailer so order is: header → steps → pre/post → overall.
    if result.pre_summary:
        print("", flush=True)
        print(result.pre_summary, flush=True)
    print(_format_trailer(plan, result), flush=True)
    return 0 if result.success else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
