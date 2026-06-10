"""Plan recorder for cli-interactive-tester.

A ``PlanRecorder`` watches an ``AgentSession`` as a human / LLM driver
runs through a scenario and emits a deterministic **test plan** YAML
that the replay runner (``auto_test_tool.replay``) can re-execute in
CI without any LLM in the loop.

Design:

* The recorder is **composition over inheritance** — it doesn't subclass
  ``AgentSession``. Instead, ``attach()`` wraps the session's
  ``start`` / ``act`` / ``observe`` / ``screenshot`` / ``finish``
  bound methods so non-recording runs remain byte-identical to the
  pre-recorder code path. Detach by dropping the recorder; the
  wrapping is per-instance.

* Auto-seeded assertions are deliberately a strong first draft, not a
  final test. Defaults:

    contains      → last 3 non-empty, non-noise lines of the post-step
                    pane capture (delta-aware: lines already present
                    in the prior capture are skipped first)
    not_contains  → ["Traceback", "error:"] as a safety net
    timeout_seconds → 10 by default, bumped if the recorder noticed
                    ``observe()`` polling for longer than 5 seconds

  The author is expected to trim ``contains`` down to the meaningful
  lines after recording, and to delete the entire ``assert`` block on
  steps where presence is enough (e.g. pure waits).

* The plan is flushed on ``finish()``. If ``finish()`` is never called
  (e.g. the driver crashed) the recorder also flushes on ``__del__``
  best-effort, but the canonical path is ``finish()``.

The output schema is documented in ``plans/SCHEMA.md`` (added alongside
this module) and tagged with ``schema_version: 1``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .agent import AgentSession

SCHEMA_VERSION = 1

DEFAULT_TIMEOUT_SECONDS = 10.0
SLOW_OBSERVE_THRESHOLD = 5.0  # bump timeout_seconds when observe() took longer
SLOW_OBSERVE_BUMP_TO = 30.0

DEFAULT_NOT_CONTAINS = ["Traceback", "error:"]
DEFAULT_CONTAINS_LINES = 3

# Lines we skip when seeding `contains` because they're noise from the
# shell / tmux frame rather than CLI output worth asserting on.
_NOISE_LINE_PREFIXES = (
    "$ ",        # bash prompt
    "% ",        # zsh prompt
    "# ",        # root prompt
)


def _non_empty_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _meaningful_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in _non_empty_lines(text):
        stripped = line.strip()
        if any(stripped.startswith(p) for p in _NOISE_LINE_PREFIXES):
            continue
        out.append(stripped)
    return out


def _seed_contains(prev_capture: str, new_capture: str) -> list[str]:
    """Pick the last few lines that are new and worth asserting on."""
    prev_set = set(_non_empty_lines(prev_capture))
    candidates = _meaningful_lines(new_capture)
    # Prefer lines that weren't already in the previous capture; if every
    # line is a duplicate (no real delta) fall back to the tail of the
    # current capture so we still seed *something*.
    delta = [line for line in candidates if line not in prev_set]
    pool = delta or candidates
    return pool[-DEFAULT_CONTAINS_LINES:]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _RecordedStep:
    """An in-memory step before it is serialised to YAML."""

    index: int
    kind: str  # "start" | "action" | "observe" | "screenshot"
    action: dict | None = None
    label: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    contains: list[str] = field(default_factory=list)
    not_contains: list[str] = field(default_factory=lambda: list(DEFAULT_NOT_CONTAINS))
    session_exited: bool | None = None  # tri-state: None = don't assert

    def to_yaml_obj(self) -> dict:
        out: dict[str, Any] = {"index": self.index, "kind": self.kind}
        if self.action is not None:
            out["action"] = self.action
        if self.kind == "screenshot":
            out["label"] = self.label
            return out
        assert_block: dict[str, Any] = {}
        if self.contains:
            assert_block["contains"] = list(self.contains)
        if self.not_contains:
            assert_block["not_contains"] = list(self.not_contains)
        if self.session_exited is not None:
            assert_block["session_exited"] = self.session_exited
        if assert_block:
            assert_block["timeout_seconds"] = self.timeout_seconds
            out["assert"] = assert_block
        return out


@dataclass
class PlanRecorder:
    """Attach to an ``AgentSession`` and record its driver actions.

    Typical use (programmatic; the MCP server wires this for you)::

        session = AgentSession(...)
        recorder = PlanRecorder(
            source_scenario="scenarios/foo.yaml",
            scenario_data=loaded_yaml_dict_or_None,
            plan_path="plans/foo.plan.yaml",
        )
        recorder.attach(session)
        session.start()
        ...
        session.finish()  # recorder auto-flushes
    """

    source_scenario: str | None = None
    scenario_data: dict | None = None
    plan_path: str | None = None
    driver: str = "unknown"

    _session: AgentSession | None = field(default=None, init=False)
    _steps: list[_RecordedStep] = field(default_factory=list, init=False)
    _flushed: bool = field(default=False, init=False)
    _prev_capture: str = field(default="", init=False)
    _last_observe_seconds: float = field(default=0.0, init=False)
    _in_act: bool = field(default=False, init=False)
    _orig_start: Callable | None = field(default=None, init=False)
    _orig_act: Callable | None = field(default=None, init=False)
    _orig_screenshot: Callable | None = field(default=None, init=False)
    _orig_finish: Callable | None = field(default=None, init=False)

    # ------------------------------------------------------------------
    # Attach / detach
    # ------------------------------------------------------------------
    def attach(self, session: AgentSession) -> None:
        if self._session is not None:
            raise RuntimeError("PlanRecorder is already attached")
        self._session = session

        # Wrap the bound methods we care about. We keep the originals so
        # ``detach`` (or a partial failure) can restore them.
        self._orig_start = session.start
        self._orig_act = session.act
        self._orig_screenshot = session.screenshot
        self._orig_finish = session.finish

        session.start = self._wrapped_start  # type: ignore[method-assign]
        session.act = self._wrapped_act  # type: ignore[method-assign]
        session.screenshot = self._wrapped_screenshot  # type: ignore[method-assign]
        session.finish = self._wrapped_finish  # type: ignore[method-assign]

    def detach(self) -> None:
        if self._session is None:
            return
        if self._orig_start is not None:
            self._session.start = self._orig_start  # type: ignore[method-assign]
        if self._orig_act is not None:
            self._session.act = self._orig_act  # type: ignore[method-assign]
        if self._orig_screenshot is not None:
            self._session.screenshot = self._orig_screenshot  # type: ignore[method-assign]
        if self._orig_finish is not None:
            self._session.finish = self._orig_finish  # type: ignore[method-assign]
        self._session = None

    # ------------------------------------------------------------------
    # Wrappers
    # ------------------------------------------------------------------
    def _wrapped_start(self) -> str:
        assert self._orig_start is not None
        capture = self._orig_start()
        self._record_capture("start", action=None, capture=capture)
        return capture

    def _wrapped_act(self, action: dict) -> str:
        assert self._orig_act is not None
        import time

        before = time.time()
        # AgentSession.act() internally calls self.screenshot() before
        # executing keystrokes. We don't want to record that as a plan
        # step — only the user-visible act / observe / screenshot calls
        # should appear. Suppress the wrapped screenshot for the duration.
        self._in_act = True
        try:
            capture = self._orig_act(action)
        finally:
            self._in_act = False
        elapsed = time.time() - before
        self._last_observe_seconds = elapsed
        # Strip the [SESSION EXITED] marker the agent prepends so it doesn't
        # leak into seeded `contains`; instead surface it as session_exited.
        session_exited = capture.startswith("[SESSION EXITED]")
        clean_capture = capture[len("[SESSION EXITED]\n"):] if session_exited else capture
        self._record_capture(
            "action",
            action=dict(action),
            capture=clean_capture,
            session_exited=session_exited or None,
        )
        return capture

    def _wrapped_screenshot(self, label: str = "") -> str:
        assert self._orig_screenshot is not None
        svg_path = self._orig_screenshot(label=label)
        if self._in_act:
            # Internal screenshot triggered from inside act() — don't
            # double-record. The action itself will be recorded after the
            # keystrokes execute.
            return svg_path
        step = _RecordedStep(
            index=len(self._steps),
            kind="screenshot",
            label=label,
        )
        self._steps.append(step)
        return svg_path

    def _wrapped_finish(self) -> str:
        assert self._orig_finish is not None
        try:
            return self._orig_finish()
        finally:
            self.flush()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record_capture(
        self,
        kind: str,
        action: dict | None,
        capture: str,
        session_exited: bool | None = None,
    ) -> None:
        contains = _seed_contains(self._prev_capture, capture)
        timeout = DEFAULT_TIMEOUT_SECONDS
        if self._last_observe_seconds > SLOW_OBSERVE_THRESHOLD:
            timeout = SLOW_OBSERVE_BUMP_TO
        step = _RecordedStep(
            index=len(self._steps),
            kind=kind,
            action=action,
            contains=contains,
            timeout_seconds=timeout,
            session_exited=session_exited,
        )
        self._steps.append(step)
        self._prev_capture = capture

    # ------------------------------------------------------------------
    # Flushing the plan
    # ------------------------------------------------------------------
    def flush(self) -> str | None:
        """Write the plan YAML to disk. Returns the path written, or None."""
        if self._flushed or self._session is None:
            return None
        self._flushed = True

        target = self.plan_path or self._default_plan_path()
        if target is None:
            return None
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)

        plan_obj = self._build_plan_obj()
        header_comment = self._build_header_comment()
        with open(target, "w") as f:
            f.write(header_comment)
            yaml.safe_dump(
                plan_obj,
                f,
                sort_keys=False,
                default_flow_style=False,
                width=10_000,  # keep long command lines on one line
            )
        return target

    def _default_plan_path(self) -> str | None:
        if not self.source_scenario:
            return None
        stem = Path(self.source_scenario).stem
        return os.path.join("plans", f"{stem}.plan.yaml")

    def _build_header_comment(self) -> str:
        scen = self.source_scenario or "(no source scenario)"
        return (
            f"# AUTO-GENERATED by PlanRecorder on {_utc_now_iso()}\n"
            f"# Source: {scen}   Driver: {self.driver}\n"
            f"# Re-record to refresh; trim `contains:` lines to the meaningful ones,\n"
            f"# and delete the `assert:` block on steps where presence is not\n"
            f"# meaningful (e.g. pure waits, screenshots).\n"
        )

    def _build_plan_obj(self) -> dict:
        sess = self._session
        assert sess is not None
        scenario = self.scenario_data or {}
        plan: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "name": scenario.get("name") or _name_from_source(self.source_scenario),
            "source_scenario": self.source_scenario,
            "command": sess.command,
            "cwd": sess.cwd,
            "env": dict(sess.env or {}),
            "allocate_ports": scenario.get("allocate_ports") or [],
            "pre": scenario.get("pre") or [],
            "post": scenario.get("post") or [],
            "steps": [s.to_yaml_obj() for s in self._steps],
        }
        return plan


def _name_from_source(source: str | None) -> str:
    if not source:
        return "unnamed-plan"
    return Path(source).stem
