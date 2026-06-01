#!/usr/bin/env python3
"""
MCP server for terminal-driven CLI testing.

Exposes tmux-backed terminal control as MCP tools so that Copilot CLI
(or any MCP client) can drive interactive CLI sessions — observing terminal
state and sending actions, similar to Playwright MCP for browsers.

Tools:
    start_session  — Launch a command in tmux
    observe        — Read current terminal text
    send_action    — Send keystrokes (select, confirm, input, etc.)
    screenshot     — Capture terminal → SVG
    finish_session — Tear down session and generate HTML report

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

mcp = FastMCP(
    "azd-test-tool",
    instructions="""You are driving an interactive CLI session running in a tmux terminal.

Your workflow:
1. Read the scenario file (if provided) to understand the goals.
2. Call start_session to launch the command.
3. Loop: call observe to see the terminal, then call send_action to interact.
4. When the task is complete, call finish_session to generate a report.

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


def _get_session(session_id: str) -> AgentSession:
    """Get an active session or raise an error."""
    if session_id not in _sessions:
        active = list(_sessions.keys()) or ["(none)"]
        raise ValueError(
            f"No active session '{session_id}'. Active sessions: {', '.join(active)}"
        )
    return _sessions[session_id]


@mcp.tool()
def start_session(
    command: str,
    cwd: str | None = None,
    session_id: str = "default",
    env: dict[str, str] | None = None,
    output_dir: str = "reports",
    run_name: str | None = None,
) -> str:
    """Start an interactive CLI command in a tmux terminal session.

    Args:
        command: The CLI command to run (e.g. "azd ai agent init").
        cwd: Working directory. Supports ~ expansion. Created if it doesn't exist.
             If not provided, a temp directory under /tmp is created automatically.
        session_id: Identifier for this session (allows multiple concurrent sessions).
        env: Extra environment variables to set.
        output_dir: Where to store screenshots and the final HTML report.
        run_name: Name for the run folder (e.g. scenario name). Defaults to a timestamp.

    Returns:
        The initial terminal text after the command starts.
    """
    if not tmux_is_installed():
        return "ERROR: tmux is required but not installed. Install with: brew install tmux"

    if session_id in _sessions:
        return f"ERROR: Session '{session_id}' is already active. Finish it first or use a different session_id."

    session = AgentSession(
        command=command,
        cwd=cwd,
        env=env or {},
        output_dir=output_dir,
        run_name=run_name,
    )
    initial_state = session.start()
    _sessions[session_id] = session

    return (
        f"Session '{session_id}' started.\n"
        f"Command: {command}\n"
        f"Working directory: {os.path.expanduser(cwd)}\n"
        f"Screenshots: {session.run_dir}\n\n"
        f"--- Terminal ---\n{initial_state}"
    )


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
    an HTML report with all captured screenshots embedded.

    Args:
        session_id: Which session to finish.

    Returns:
        Path to the generated HTML report.
    """
    session = _get_session(session_id)
    report_path = session.finish()
    del _sessions[session_id]
    return f"Session finished. Report: {report_path}"


@mcp.tool()
def load_scenario(path: str) -> str:
    """Read a scenario YAML file and return its goals and configuration.

    Call this first to understand what the scenario wants you to do,
    then use start_session with the command/cwd/env from the scenario.

    Args:
        path: Path to the YAML scenario file (absolute or relative, supports ~).

    Returns:
        Structured summary of the scenario: name, command, cwd, env, and goals.
    """
    return _read_scenario_file(path)


def _read_scenario_file(path: str) -> str:
    """Internal: parse a scenario YAML and return a structured summary."""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        return f"ERROR: Scenario file not found: {expanded}"

    with open(expanded) as f:
        data = yaml.safe_load(f)

    parts = [f"Scenario: {data.get('name', '(unnamed)')}"]
    parts.append(f"Command: {data.get('command', '(none)')}")
    if data.get("cwd"):
        parts.append(f"Working directory: {data['cwd']}")
    if data.get("env"):
        parts.append(f"Environment: {data['env']}")

    if data.get("goals"):
        parts.append("\nGoals:")
        for i, goal in enumerate(data["goals"], 1):
            parts.append(f"  {i}. {goal}")
    elif data.get("goal"):
        parts.append(f"\nGoal:\n{data['goal']}")
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
