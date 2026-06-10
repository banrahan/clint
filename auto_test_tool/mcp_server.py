#!/usr/bin/env python3
"""
MCP server for terminal-driven CLI testing.

Exposes tmux-backed terminal control as MCP tools so that Copilot CLI
(or any MCP client) can drive interactive CLI sessions — observing terminal
state and sending actions, similar to Playwright MCP for browsers.

Tools:
    load_scenario          — Read a scenario YAML and summarize goals + pre/post hooks
    run_pre_hooks          — Execute the scenario's `pre:` host shell hooks (setup)
    start_session          — Launch a command in tmux
    observe                — Read current terminal text
    send_action            — Send keystrokes (select, confirm, input, etc.)
    screenshot             — Capture terminal → SVG
    report_finding         — Record a bug / UX issue with a screenshot
    finish_session         — Tear down session and generate HTML report
    run_post_hooks         — Execute the scenario's `post:` host shell hooks (cleanup)
    release_scenario_ports — Drop the shared port pool for a scenario (defensive)

Resources:
    scenario://{path} — Read a scenario YAML file

Run with:
    python -m auto_test_tool.mcp_server
"""

import os
import yaml
from mcp.server.fastmcp import FastMCP

from .agent import AgentSession
from .runner import tmux_is_installed
from .hooks import Hook, execute_hooks, format_hook_results, parse_hooks
from .ports import PortPool, get_pool, merged_vars, release_pool
from .recorder import PlanRecorder
from .template import substitute_in_mapping, substitute_template

mcp = FastMCP(
    "azd-test-tool",
    instructions="""You are driving an interactive CLI session running in a tmux terminal.

Your workflow:
1. Read the scenario file (if provided) to understand the goals.
2. If the scenario lists pre hooks, call run_pre_hooks before launching the CLI.
3. Call start_session to launch the command.
4. Loop: call observe to see the terminal, then call send_action to interact.
5. When the task is complete, call finish_session to generate a report.
6. If the scenario lists post hooks, call run_post_hooks after finish_session.

Tips for interpreting the terminal:
- Lines with ">" or "❯" indicate the currently selected item in a list.
- "? " at the start of a line is a prompt question.
- Use select (choice_index) for list prompts — index 0 is the first visible item.
- Use select (choice_text) to pick a specific option by its label.
- Use confirm (value: true/false) for yes/no prompts.
- Use input (text) for free-text input fields. Empty string accepts the default.
- Use wait to pause and let the CLI process (e.g., after a long operation).
- After each action, call observe to see the result before deciding the next step.
""",
)

# Active sessions keyed by session_id (support multiple concurrent sessions)
_sessions: dict[str, AgentSession] = {}
# Map session_id -> scenario_path so finish_session can refcount-release the
# scenario's PortPool when the last session referencing it tears down.
_session_scenarios: dict[str, str] = {}
# Pending recorders keyed by session_id. ``record_plan`` populates this;
# ``start_session`` consumes the entry (if any) and attaches the recorder
# to the new AgentSession before calling ``start()``.
_pending_recorders: dict[str, PlanRecorder] = {}
# Active recorders keyed by session_id (so finish_session can report the
# plan file path back to the caller).
_active_recorders: dict[str, PlanRecorder] = {}


def _get_session(session_id: str) -> AgentSession:
    """Get an active session or raise an error."""
    if session_id not in _sessions:
        active = list(_sessions.keys()) or ["(none)"]
        raise ValueError(
            f"No active session '{session_id}'. Active sessions: {', '.join(active)}"
        )
    return _sessions[session_id]


def _resolve_vars(
    scenario_path: str | None,
    extra: dict[str, str] | None,
    instance_id: str | None = None,
) -> tuple[dict[str, object], PortPool | None]:
    """Build the template-var dict for one MCP call.

    Loads the scenario (if any), gets-or-creates its ``PortPool``, and
    merges in any caller-supplied ``extra`` vars. Returns the merged
    dict plus the pool (so callers can format an "Allocated ports" block).

    Keying rules:

    * ``instance_id=None`` (default) — pool key is the scenario path
      alone. Run + invoke of one instance share the same pool. The
      ``{instance}`` template var defaults to ``"main"`` so scenarios
      that interpolate it (e.g. ``cwd: '/tmp/run-{instance}'``) still
      resolve cleanly in single-instance use.
    * ``instance_id="<tag>"`` — pool key is ``f"{path}#{tag}"`` so N
      parallel runs of one scenario get N independent pools. The
      ``{instance}`` template var is set to ``<tag>``.

    Explicit ``session_vars["instance"]`` always wins over both
    defaults so callers can override.
    """
    if not scenario_path:
        return dict(extra or {}), None
    expanded, data, err = _load_scenario_data(scenario_path)
    if err or data is None:
        return dict(extra or {}), None
    pool_key = f"{expanded}#{instance_id}" if instance_id else expanded
    pool = get_pool(pool_key, data.get("allocate_ports"))
    base_vars = merged_vars(pool, extra)
    # Auto-inject {instance} so scenarios can interpolate it in cwd/env/etc
    # without forcing callers to pass session_vars for the common case.
    base_vars.setdefault("instance", instance_id or "main")
    return base_vars, pool


def _pool_key_for_session(scenario_path: str, instance_id: str | None) -> str:
    """Mirror the keying used in ``_resolve_vars`` for refcount release."""
    expanded = os.path.expanduser(scenario_path)
    return f"{expanded}#{instance_id}" if instance_id else expanded


def _format_allocated_ports(pool: PortPool | None) -> str:
    if not pool or not pool.names:
        return ""
    parts = [f"{name}={pool.get(name)}" for name in pool.names]
    return "Allocated ports: " + ", ".join(parts)


@mcp.tool()
def start_session(
    command: str,
    cwd: str | None = None,
    session_id: str = "default",
    env: dict[str, str] | None = None,
    output_dir: str = "reports",
    run_name: str | None = None,
    scenario_path: str | None = None,
    session_vars: dict[str, str] | None = None,
    instance_id: str | None = None,
) -> str:
    """Start an interactive CLI command in a tmux terminal session.

    Args:
        command: The CLI command to run (e.g. "azd ai agent init").
                 May contain ``{name}`` placeholders resolved from the
                 scenario's allocated ports (see ``allocate_ports`` in
                 the scenario YAML) and ``session_vars``.
        cwd: Working directory. Supports ~ expansion. Created if it doesn't exist.
             If not provided, a temp directory under /tmp is created automatically.
             Also subject to ``{name}`` substitution.
        session_id: Identifier for this session (allows multiple concurrent sessions).
        env: Extra environment variables to set. Values are substituted.
        output_dir: Where to store screenshots and the final HTML report.
        run_name: Name for the run folder (e.g. scenario name). Defaults to a timestamp.
        scenario_path: When provided, share the scenario's ``PortPool`` with
            other sessions of the same scenario so e.g. ``azd ai agent run``
            and ``azd ai agent invoke --local`` see the same ``{port}``.
        session_vars: Extra ``{name}`` substitutions for this call only.
        instance_id: Tag for parallel instances of the SAME scenario.
            When set, this session gets its own ``PortPool`` (keyed by
            ``scenario_path + instance_id``) so 3 parallel runs of one
            scenario don't share the same ``{port}``. The id is also
            available as ``{instance}`` in templated strings — useful for
            making ``cwd`` and resource names unique across instances.

    Returns:
        The initial terminal text after the command starts, prefixed with
        the allocated port assignments when the scenario declared any.
    """
    if not tmux_is_installed():
        return "ERROR: tmux is required but not installed. Install with: brew install tmux"

    if session_id in _sessions:
        return f"ERROR: Session '{session_id}' is already active. Finish it first or use a different session_id."

    try:
        vars_dict, pool = _resolve_vars(scenario_path, session_vars, instance_id)
        # Resolve cwd first (using base vars like {instance}/{port}) so we can
        # expose it as {cwd} to command/env. Explicit session_vars["cwd"] still
        # wins because vars_dict was built before we get here.
        resolved_cwd = substitute_template(cwd, vars_dict) if cwd else cwd
        if resolved_cwd is not None:
            vars_dict.setdefault("cwd", resolved_cwd)
        resolved_command = substitute_template(command, vars_dict)
        resolved_env = substitute_in_mapping(env or {}, vars_dict)
    except KeyError as e:
        return f"ERROR: {e}"

    session = AgentSession(
        command=resolved_command,
        cwd=resolved_cwd,
        env=resolved_env,
        output_dir=output_dir,
        run_name=run_name,
        session_id=session_id,
    )

    # If record_plan was called for this session_id earlier, attach the
    # recorder before start() so the initial capture is recorded too.
    recorder = _pending_recorders.pop(session_id, None)
    if recorder is not None:
        if recorder.scenario_data is None and scenario_path:
            _expanded, data, _err = _load_scenario_data(scenario_path)
            if data is not None:
                recorder.scenario_data = data
        if recorder.source_scenario is None and scenario_path:
            recorder.source_scenario = scenario_path
        recorder.attach(session)
        _active_recorders[session_id] = recorder

    initial_state = session.start()
    _sessions[session_id] = session
    if scenario_path:
        _session_scenarios[session_id] = _pool_key_for_session(
            scenario_path, instance_id
        )

    ports_line = _format_allocated_ports(pool)
    header = (
        f"Session '{session_id}' started.\n"
        f"Command: {resolved_command}\n"
        f"Working directory: {os.path.expanduser(session.cwd)}\n"
        f"Screenshots: {session.run_dir}\n"
    )
    if instance_id:
        header += f"Instance: {instance_id}\n"
    if ports_line:
        header += ports_line + "\n"
    return header + f"\n--- Terminal ---\n{initial_state}"


@mcp.tool()
def observe(session_id: str = "default") -> str:
    """Capture and return the current terminal text.

    Call this after send_action to see the result, or any time you need
    to check what the CLI is currently showing.

    Args:
        session_id: Which session to observe.

    Returns:
        Plain text content of the terminal.
    """
    session = _get_session(session_id)
    return session.observe()


@mcp.tool()
def send_action(
    action: str,
    choice_index: int | None = None,
    choice_text: str | None = None,
    value: bool | None = None,
    text: str | None = None,
    toggle_indices: list[int] | None = None,
    seconds: float | None = None,
    key: str | None = None,
    count: int = 1,
    session_id: str = "default",
) -> str:
    """Send an action to the interactive CLI prompt.

    Action types:
        select       — Navigate a list and press Enter.
                       Use choice_index (0-based) OR choice_text (match by label).
        confirm      — Answer a yes/no prompt. Set value to true or false.
        input        — Type text into a free-text field and press Enter.
                       Empty string accepts the default value.
        multi_select — Toggle items in a multi-select list, then press Enter.
                       Provide toggle_indices as a list of 0-based positions.
        key          — Send raw key(s) to the terminal. Use tmux key names:
                       BSpace, Escape, C-c, Up, Down, Left, Right, Tab, etc.
                       Use count to repeat (e.g. key="BSpace", count=5).
        wait         — Pause for a number of seconds (default 2) without sending keys.

    Args:
        action: One of "select", "confirm", "input", "multi_select", "key", "wait".
        choice_index: For select — 0-based index of the item to pick.
        choice_text: For select — text label to match (scrolls down to find it).
        value: For confirm — true for yes, false for no.
        text: For input — the text to type.
        toggle_indices: For multi_select — list of 0-based indices to toggle.
        key: For key — tmux key name (BSpace, Escape, C-c, Up, Down, etc.).
        count: For key — number of times to send the key (default 1).
        seconds: For wait — how long to pause.
        session_id: Which session to act on.

    Returns:
        The terminal text after the action completes.
    """
    session = _get_session(session_id)

    # Build the action dict expected by AgentSession
    action_dict: dict = {"action": action}

    if action == "select":
        if choice_text is not None:
            action_dict = {"action": "select_by_text", "text": choice_text}
        elif choice_index is not None:
            action_dict["choice_index"] = choice_index
        else:
            action_dict["choice_index"] = 0

    elif action == "confirm":
        action_dict["value"] = value if value is not None else True

    elif action == "input":
        action_dict["text"] = text if text is not None else ""

    elif action == "multi_select":
        action_dict["toggle_indices"] = toggle_indices or [0]

    elif action == "key":
        action_dict["key"] = key or "BSpace"
        action_dict["count"] = count

    elif action == "wait":
        action_dict["seconds"] = seconds if seconds is not None else 2

    else:
        return f"ERROR: Unknown action '{action}'. Use: select, confirm, input, multi_select, key, wait."

    new_state = session.act(action_dict)
    # Surface action errors to the caller — otherwise LookupError etc. get
    # buried in the step result and the agent sees only the terminal state.
    last_step = session.result.steps[-1] if session.result.steps else None
    if last_step and last_step.error:
        return f"ERROR during {action!r}: {last_step.error}\n\n--- Terminal ---\n{new_state}"
    return new_state


@mcp.tool()
def screenshot(label: str = "", session_id: str = "default") -> str:
    """Capture the terminal as an SVG screenshot.

    Args:
        label: Optional label for the screenshot (used in the HTML report).
        session_id: Which session to capture.

    Returns:
        Path to the saved SVG file.
    """
    session = _get_session(session_id)
    svg_path = session.screenshot(label=label)
    return f"Screenshot saved: {svg_path}"


@mcp.tool()
def report_finding(
    title: str,
    description: str = "",
    category: str = "bug",
    session_id: str = "default",
) -> str:
    """Report a finding during the session.

    Automatically captures a screenshot at the current terminal state.
    Findings are collected and displayed in the final HTML report.

    Args:
        title: Short summary of the finding (e.g. "Crash on empty input").
        description: Longer explanation of what happened and expected behavior.
        category: One of "bug", "ux-issue", "observation".
        session_id: Which session to report against.

    Returns:
        Confirmation with the screenshot path.
    """
    session = _get_session(session_id)
    svg_path = session.report_finding(
        title=title,
        description=description,
        category=category,
    )
    finding_count = len(session.result.findings)
    return f"Finding #{finding_count} reported: {title} [{category}]\nScreenshot: {svg_path}"


@mcp.tool()
def finish_session(session_id: str = "default") -> str:
    """Stop the terminal session and generate an HTML report.

    This kills the tmux session, captures a final screenshot, and produces
    an HTML report with all captured screenshots embedded. When the last
    session for a scenario finishes, its shared ``PortPool`` is released.

    Args:
        session_id: Which session to finish.

    Returns:
        Path to the generated HTML report.
    """
    session = _get_session(session_id)
    report_path = session.finish()
    del _sessions[session_id]

    recorder = _active_recorders.pop(session_id, None)
    plan_line = ""
    if recorder is not None:
        # finish() already triggered the recorder's wrapped flush; surface
        # the resolved path so the caller knows where to commit it.
        plan_target = recorder.plan_path or recorder._default_plan_path()
        if plan_target:
            plan_line = f"\nPlan written: {plan_target}"

    scenario_path = _session_scenarios.pop(session_id, None)
    # Refcount: release the pool only when no other active session is using it.
    if scenario_path and scenario_path not in _session_scenarios.values():
        release_pool(scenario_path)

    return f"Session finished. Report: {report_path}{plan_line}"


@mcp.tool()
def release_scenario_ports(scenario_path: str) -> str:
    """Drop the shared ``PortPool`` for a scenario.

    ``finish_session`` already releases pools automatically when the last
    session for a scenario tears down; call this only when you've abandoned
    a scenario without finishing every session (e.g. an error path).

    Args:
        scenario_path: Path passed earlier to ``start_session`` /
            ``load_scenario``.
    """
    expanded = os.path.expanduser(scenario_path)
    released = release_pool(expanded)
    return (
        f"Released port pool for {expanded}."
        if released
        else f"No port pool was registered for {expanded}."
    )


@mcp.tool()
def record_plan(
    plan_path: str | None = None,
    scenario_path: str | None = None,
    session_id: str = "default",
    driver: str = "copilot-cli",
) -> str:
    """Arm the next ``start_session`` call to record a deterministic test plan.

    Call this **before** ``start_session`` for the same ``session_id``.
    When the session is started, a ``PlanRecorder`` attaches and captures
    every ``act`` / ``observe`` / ``screenshot`` call into a YAML file
    under ``plans/`` that the ``cli-tester-replay`` runner can re-execute
    in CI without any LLM in the loop.

    Args:
        plan_path: Where to write the plan. Defaults to
            ``plans/<scenario-stem>.plan.yaml`` when ``scenario_path`` is
            provided; required otherwise.
        scenario_path: Path to the source scenario YAML — used to pre-fill
            ``allocate_ports`` / ``pre`` / ``post`` / ``name`` in the plan.
        session_id: Which session this recording is for. Must match the
            ``session_id`` passed to ``start_session``.
        driver: Free-text label recorded in the plan header (e.g.
            ``"copilot-cli"`` or your name). Default: ``copilot-cli``.

    Returns:
        Confirmation including the resolved plan path.
    """
    if session_id in _active_recorders:
        return f"ERROR: Session '{session_id}' is already recording a plan."
    if session_id in _pending_recorders:
        return f"ERROR: A plan recording is already pending for session '{session_id}'."

    scenario_data: dict | None = None
    if scenario_path:
        _expanded, data, err = _load_scenario_data(scenario_path)
        if err:
            return err
        scenario_data = data

    recorder = PlanRecorder(
        source_scenario=scenario_path,
        scenario_data=scenario_data,
        plan_path=plan_path,
        driver=driver,
    )
    resolved_path = plan_path or recorder._default_plan_path()
    if not resolved_path:
        return (
            "ERROR: plan_path is required when scenario_path is not provided "
            "(no way to derive a default plan filename)."
        )
    recorder.plan_path = resolved_path
    _pending_recorders[session_id] = recorder
    return (
        f"Plan recording armed for session '{session_id}'.\n"
        f"Plan will be written to: {resolved_path}\n"
        f"Call start_session next; the recorder will attach automatically."
    )


@mcp.tool()
def load_scenario(path: str) -> str:
    """Read a scenario YAML file and return its goals and configuration.

    Call this first to understand what the scenario wants you to do,
    then use start_session with the command/cwd/env from the scenario.
    If the scenario declares pre/post hooks, also call run_pre_hooks before
    start_session and run_post_hooks after finish_session.

    Args:
        path: Path to the YAML scenario file (absolute or relative, supports ~).

    Returns:
        Structured summary of the scenario: name, command, cwd, env, and goals.
    """
    return _read_scenario_file(path)


@mcp.tool()
def run_pre_hooks(scenario_path: str) -> str:
    """Execute the scenario's `pre` host shell hooks in order.

    Call this after load_scenario and before start_session when the scenario
    declares a `pre:` list. Hooks run on the host (not inside tmux). Execution
    is fail-fast unless a hook sets `continue_on_error: true`; remaining hooks
    after an abort are marked SKIPPED.

    Args:
        scenario_path: Path to the YAML scenario file.

    Returns:
        Human-readable summary of each hook's status, exit code, and output tails.
    """
    return _run_phase(scenario_path, "pre")


@mcp.tool()
def run_post_hooks(scenario_path: str) -> str:
    """Execute the scenario's `post` host shell hooks in order.

    Call this after finish_session when the scenario declares a `post:` list
    (typically cleanup). Same execution semantics as run_pre_hooks.

    Args:
        scenario_path: Path to the YAML scenario file.

    Returns:
        Human-readable summary of each hook's status, exit code, and output tails.
    """
    return _run_phase(scenario_path, "post")


def _load_scenario_data(path: str) -> tuple[str, dict | None, str | None]:
    """Return (expanded_path, data_or_None, error_or_None)."""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        return expanded, None, f"ERROR: Scenario file not found: {expanded}"
    with open(expanded) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return expanded, None, f"ERROR: Scenario root must be a mapping, got {type(data).__name__}"
    return expanded, data, None


def _run_phase(scenario_path: str, phase: str, instance_id: str | None = None) -> str:
    expanded, data, err = _load_scenario_data(scenario_path)
    if err:
        return err
    assert data is not None
    raw = data.get(phase)
    try:
        hooks = parse_hooks(raw, scenario_cwd=data.get("cwd"))
    except ValueError as e:
        return f"ERROR: Invalid {phase} hooks in scenario: {e}"
    if not hooks:
        return f"No {phase} hooks declared in scenario."

    # Apply ``{name}`` substitution to hook ``run`` / ``cwd`` / ``env`` values
    # using the scenario's shared port pool. Substitution happens at this
    # boundary so hooks.py stays agnostic of scenario-level concerns.
    vars_dict, _ = _resolve_vars(scenario_path, None, instance_id)
    # Expose the scenario's resolved cwd as {cwd} so hooks can reference the
    # same working directory the session will run in.
    scenario_cwd = data.get("cwd")
    if scenario_cwd:
        try:
            vars_dict.setdefault("cwd", substitute_template(scenario_cwd, vars_dict))
        except KeyError:
            pass
    try:
        resolved: list[Hook] = []
        for h in hooks:
            resolved.append(
                Hook(
                    run=substitute_template(h.run, vars_dict),
                    cwd=substitute_template(h.cwd, vars_dict) if h.cwd else h.cwd,
                    explicit_cwd=h.explicit_cwd,
                    env=substitute_in_mapping(h.env, vars_dict),
                    continue_on_error=h.continue_on_error,
                    timeout=h.timeout,
                    name=h.name,
                )
            )
    except KeyError as e:
        return f"ERROR: Invalid placeholder in {phase} hook: {e}"

    results = execute_hooks(resolved)
    return format_hook_results(phase, results)


def _read_scenario_file(path: str) -> str:
    """Internal: parse a scenario YAML and return a structured summary."""
    expanded, data, err = _load_scenario_data(path)
    if err:
        return err
    assert data is not None

    # Materialise the shared port pool (if any) so the goals/command shown
    # here use the same allocated values that ``start_session`` will see.
    try:
        vars_dict, pool = _resolve_vars(path, None, None)
    except ValueError as e:
        return f"ERROR: Invalid allocate_ports: {e}"

    # Expose the scenario's cwd as {cwd} so display matches what start_session
    # will produce.
    scenario_cwd = data.get("cwd")
    if scenario_cwd:
        try:
            vars_dict.setdefault("cwd", substitute_template(scenario_cwd, vars_dict))
        except KeyError:
            pass

    def sub(s: object) -> str:
        if not isinstance(s, str):
            return str(s)
        try:
            return substitute_template(s, vars_dict)
        except KeyError:
            # Don't refuse to display the scenario just because a placeholder
            # references something only available at runtime via session_vars.
            return s

    parts = [f"Scenario: {data.get('name', '(unnamed)')}"]
    parts.append(f"Command: {sub(data.get('command', '(none)'))}")
    if data.get("cwd"):
        parts.append(f"Working directory: {sub(data['cwd'])}")
    if data.get("env"):
        parts.append(f"Environment: {data['env']}")
    if pool.names:
        ports_line = ", ".join(f"{name}={pool.get(name)}" for name in pool.names)
        parts.append(f"Allocated ports: {ports_line}")

    for phase in ("pre", "post"):
        raw = data.get(phase)
        if not raw:
            continue
        try:
            hooks = parse_hooks(raw, scenario_cwd=data.get("cwd"))
        except ValueError as e:
            parts.append(f"\n{phase.capitalize()} hooks: INVALID — {e}")
            continue
        parts.append(
            f"\n{phase.capitalize()} hooks ({len(hooks)}) — "
            f"call run_{phase}_hooks to execute:"
        )
        for i, h in enumerate(hooks, 1):
            flags = []
            if h.continue_on_error:
                flags.append("continue_on_error")
            if h.timeout != 120:
                flags.append(f"timeout={h.timeout}s")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            parts.append(f"  {i}. {sub(h.label)}{suffix}")

    if data.get("goals"):
        parts.append("\nGoals:")
        for i, goal in enumerate(data["goals"], 1):
            parts.append(f"  {i}. {sub(goal)}")
    elif data.get("goal"):
        parts.append(f"\nGoal:\n{sub(data['goal'])}")
    elif data.get("steps"):
        parts.append("\nSteps (legacy format — treat as goals):")
        for i, step in enumerate(data["steps"], 1):
            desc = f"Wait for '{step.get('expect', '')}' then {step.get('action', 'wait')}"
            parts.append(f"  {i}. {desc}")

    return "\n".join(parts)


@mcp.resource("scenario://{path}")
def read_scenario(path: str) -> str:
    """Read a scenario YAML file and return its contents."""
    return _read_scenario_file(path)


def main():
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
