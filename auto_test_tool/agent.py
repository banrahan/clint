#!/usr/bin/env python3
"""
Agent-driven exploration mode for interactive CLI tools.

This module provides a tmux-backed terminal session that an external agent
(such as Copilot CLI via MCP) can drive interactively. The agent observes
terminal state and sends actions.

Usage:
    from auto_test_tool.agent import AgentSession

    session = AgentSession("azd ai agent init", cwd="~/agents/test")
    session.start()
    state = session.observe()
    state = session.act({"action": "select", "choice_index": 0})
    report = session.finish()
"""

import json
import os
import time
from datetime import datetime

from .runner import (
    generate_html_report,
    render_ansi_to_svg,
    save_text_capture,
    tmux_capture_pane,
    tmux_create_session,
    tmux_kill_session,
    tmux_send_keys,
    tmux_send_text,
    tmux_session_alive,
    tmux_session_name,
    BugReport,
    ScenarioResult,
    StepResult,
    ANSI_RE,
)
ACTION_SETTLE_DELAY = 2.0
POLL_INTERVAL = 0.5
PRE_ACTION_DELAY = 1.0  # wait for prompt widget to fully initialize


def _execute_agent_action(session: str, action: dict) -> None:
    """Execute an action decided by the agent."""
    act = action.get("action", "wait")

    if act == "select":
        idx = action.get("choice_index", 0)
        for _ in range(idx):
            tmux_send_keys(session, "Down")
            time.sleep(0.1)
        tmux_send_keys(session, "Enter")

    elif act == "select_by_text":
        target = action.get("text", "")
        for _ in range(25):
            capture = tmux_capture_pane(session, with_ansi=False)
            lines = capture.split("\n")
            for line in lines:
                if target.lower() in line.lower() and (">" in line or "❯" in line):
                    tmux_send_keys(session, "Enter")
                    return
            tmux_send_keys(session, "Down")
            time.sleep(0.15)
        tmux_send_keys(session, "Enter")

    elif act == "confirm":
        value = action.get("value", True)
        tmux_send_keys(session, "y" if value else "n")
        tmux_send_keys(session, "Enter")

    elif act == "input":
        text = action.get("text", "")
        tmux_send_text(session, text)
        tmux_send_keys(session, "Enter")

    elif act == "multi_select":
        indices = action.get("toggle_indices", [0])
        current = 0
        for idx in sorted(indices):
            while current < idx:
                tmux_send_keys(session, "Down")
                time.sleep(0.1)
                current += 1
            tmux_send_keys(session, " ")
            time.sleep(0.1)
        tmux_send_keys(session, "Enter")

    elif act == "wait":
        seconds = action.get("seconds", 2)
        time.sleep(seconds)

    elif act == "done":
        pass

    else:
        raise ValueError(f"Unknown agent action: {act}")


class AgentSession:
    """
    A tmux-backed terminal session that an external agent can drive.

    Usage from Copilot CLI or Python::

        session = AgentSession("azd ai agent init", cwd="~/agents/test")
        session.start()

        while not session.is_done:
            state = session.observe()  # returns terminal text
            # Agent decides what to do...
            session.act({"action": "select", "choice_index": 0})

        session.finish()  # generates report, returns report path
    """

    def __init__(
        self,
        command: str,
        cwd: str = ".",
        env: dict[str, str] | None = None,
        output_dir: str = "screenshots",
    ):
        self.command = command
        self.cwd = cwd
        self.env = env or {}
        self.env.setdefault("AZD_DISABLE_AGENT_DETECT", "1")
        self.env.setdefault("FORCE_COLOR", "1")
        self.output_dir = output_dir
        self.session_name = tmux_session_name()
        self.is_done = False
        self.step_index = 0
        self.prev_capture = ""

        run_name = f"agent_{datetime.now():%Y%m%d_%H%M%S}"
        self.run_dir = os.path.join(output_dir, run_name)

        self.result = ScenarioResult(
            name=f"Agent session: {command}",
            command=command,
            start_time=datetime.now().isoformat(),
        )

    def start(self) -> str:
        """Start the tmux session and return the initial terminal state."""
        os.makedirs(self.run_dir, exist_ok=True)
        cwd_expanded = os.path.expanduser(self.cwd)
        os.makedirs(cwd_expanded, exist_ok=True)

        tmux_create_session(self.session_name, self.command, self.cwd, self.env)
        time.sleep(2)
        return self.observe()

    def observe(self) -> str:
        """Capture and return the current terminal state as plain text.

        If the underlying process has exited, returns the final scrollback
        output prefixed with a marker so the caller knows the session ended.
        """
        # Check if the process is still running
        if not tmux_session_alive(self.session_name):
            current = tmux_capture_pane(self.session_name, with_ansi=False)
            self.prev_capture = current
            return f"[SESSION EXITED]\n{current}"

        for _ in range(10):
            current = tmux_capture_pane(self.session_name, with_ansi=False)
            if current.strip() != self.prev_capture.strip():
                break
            time.sleep(POLL_INTERVAL)
        else:
            current = tmux_capture_pane(self.session_name, with_ansi=False)

        self.prev_capture = current
        return current

    def screenshot(self, label: str = "") -> str:
        """Capture a screenshot (SVG) and return the file path."""
        ansi = tmux_capture_pane(self.session_name, with_ansi=True)
        title = label or f"Step {self.step_index}"

        svg_file = os.path.join(self.run_dir, f"step_{self.step_index:03d}.svg")
        render_ansi_to_svg(ansi, svg_file, title=title)

        txt_file = os.path.join(self.run_dir, f"step_{self.step_index:03d}.txt")
        save_text_capture(ansi, txt_file)

        return svg_file

    def act(self, action: dict) -> str:
        """
        Execute an action and return the new terminal state.

        Actions::

          {"action": "select", "choice_index": 0}
          {"action": "select_by_text", "text": "Python"}
          {"action": "confirm", "value": true}
          {"action": "input", "text": "my-agent"}
          {"action": "multi_select", "toggle_indices": [0, 2]}
          {"action": "wait", "seconds": 2}
          {"action": "done", "summary": "..."}
        """
        step_result = StepResult(
            step_index=self.step_index,
            expect_pattern="(agent-driven)",
            action=json.dumps(action),
            label=action.get("label", f"{action.get('action', '?')}"),
            timestamp=datetime.now().isoformat(),
        )
        start = time.time()

        self.screenshot(label=f"Step {self.step_index}: {action.get('action', '?')}")

        if action.get("action") == "done":
            self.is_done = True
            step_result.matched_text = action.get("summary", "Done")
            step_result.elapsed_seconds = time.time() - start
            self.result.steps.append(step_result)
            return self.prev_capture

        # Wait for the prompt widget to fully initialize before sending keys
        time.sleep(PRE_ACTION_DELAY)

        try:
            _execute_agent_action(self.session_name, action)
            time.sleep(action.get("delay_after", ACTION_SETTLE_DELAY))
        except Exception as e:
            step_result.success = False
            step_result.error = str(e)
            self.result.success = False

        step_result.elapsed_seconds = time.time() - start
        step_result.svg_path = os.path.join(
            self.run_dir, f"step_{self.step_index:03d}.svg"
        )
        self.result.steps.append(step_result)
        self.step_index += 1

        return self.observe()

    def report_bug(
        self,
        title: str,
        description: str = "",
        severity: str = "medium",
    ) -> str:
        """Record a bug found during the session. Automatically takes a screenshot."""
        svg_path = self.screenshot(label=f"Bug: {title}")
        bug = BugReport(
            step_index=self.step_index,
            title=title,
            description=description,
            severity=severity,
            screenshot_path=svg_path,
        )
        self.result.bugs.append(bug)
        return svg_path

    def finish(self) -> str:
        """Stop the session, generate report, return report path."""
        self.is_done = True

        try:
            ansi = tmux_capture_pane(self.session_name, with_ansi=True)
            final_svg = os.path.join(self.run_dir, "final.svg")
            render_ansi_to_svg(ansi, final_svg, title="Final State")
            save_text_capture(ansi, os.path.join(self.run_dir, "final.txt"))
        except Exception:
            pass

        tmux_kill_session(self.session_name)
        self.result.end_time = datetime.now().isoformat()

        result_json = os.path.join(self.run_dir, "result.json")
        with open(result_json, "w") as f:
            json.dump(
                {
                    "name": self.result.name,
                    "command": self.result.command,
                    "start_time": self.result.start_time,
                    "end_time": self.result.end_time,
                    "success": self.result.success,
                    "error": self.result.error,
                    "total_steps": len(self.result.steps),
                },
                f,
                indent=2,
            )

        generate_html_report(self.run_dir, self.result)
        return os.path.join(self.run_dir, "report.html")
