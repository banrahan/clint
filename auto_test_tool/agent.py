#!/usr/bin/env python3
"""
Agent-driven exploration mode for interactive CLI tools.

This module provides a tmux-backed terminal session that an external agent
(such as Copilot CLI) can drive interactively. The agent observes terminal
screenshots and sends actions.

Two modes:
  1. Interactive CLI: prints terminal state as JSON to stdout, reads JSON
     actions from stdin. Designed to be driven by Copilot CLI.
  2. Programmatic: import AgentSession and call start/observe/act/finish.

No API keys required — the calling agent (you, Copilot CLI) makes the decisions.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .runner import (
    generate_html_report,
    render_ansi_to_svg,
    save_text_capture,
    tmux_capture_pane,
    tmux_create_session,
    tmux_is_installed,
    tmux_kill_session,
    tmux_send_keys,
    tmux_send_text,
    tmux_session_name,
    ScenarioResult,
    StepResult,
    ANSI_RE,
)

MAX_AGENT_STEPS = 30
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
        """Capture and return the current terminal state as plain text."""
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


def main():
    """
    Interactive agent mode — reads JSON actions from stdin, prints terminal
    state to stdout. Designed to be driven by Copilot CLI or any external agent.

    Protocol:
      1. Tool prints terminal state as a JSON line to stdout
      2. Agent sends a JSON action line to stdin
      3. Tool executes action, prints new state
      4. Repeat until {"action": "done"} is received
    """
    parser = argparse.ArgumentParser(
        description="Agent-driven exploration of interactive CLI tools (driven by Copilot CLI)"
    )
    parser.add_argument(
        "command",
        help="The CLI command to run (e.g., 'azd ai agent init')",
    )
    parser.add_argument(
        "-d", "--cwd",
        default=".",
        help="Working directory to run the command in",
    )
    parser.add_argument(
        "-o", "--output",
        default="screenshots",
        help="Output directory for screenshots and reports",
    )
    parser.add_argument(
        "--env",
        nargs="*",
        default=[],
        help="Extra env vars as KEY=VALUE pairs",
    )
    args = parser.parse_args()

    if not tmux_is_installed():
        print("❌ tmux is required. Install with: brew install tmux", file=sys.stderr)
        sys.exit(1)

    env = {}
    for kv in args.env:
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k] = v

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = AgentSession(
        command=args.command,
        cwd=args.cwd,
        env=env,
        output_dir=str(output_dir),
    )

    initial_state = session.start()
    print(json.dumps({
        "type": "state",
        "step": 0,
        "terminal": initial_state,
        "message": "Session started. Send a JSON action to proceed.",
        "actions_help": {
            "select": {"choice_index": "int (0-indexed)"},
            "select_by_text": {"text": "string to match"},
            "confirm": {"value": "true/false"},
            "input": {"text": "string to type"},
            "multi_select": {"toggle_indices": "[int, ...]"},
            "wait": {"seconds": "int"},
            "done": {"summary": "string"},
        },
    }))
    sys.stdout.flush()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                action = json.loads(line)
            except json.JSONDecodeError as e:
                print(json.dumps({"type": "error", "message": f"Invalid JSON: {e}"}))
                sys.stdout.flush()
                continue

            new_state = session.act(action)

            if session.is_done:
                report_path = session.finish()
                print(json.dumps({
                    "type": "done",
                    "report": report_path,
                    "summary": action.get("summary", "Agent completed"),
                }))
                sys.stdout.flush()
                break

            svg_path = session.result.steps[-1].svg_path if session.result.steps else ""
            print(json.dumps({
                "type": "state",
                "step": session.step_index,
                "terminal": new_state,
                "screenshot": svg_path,
            }))
            sys.stdout.flush()

    except KeyboardInterrupt:
        report_path = session.finish()
        print(json.dumps({
            "type": "done",
            "report": report_path,
            "summary": "Interrupted by user",
        }))

    except Exception as e:
        session.finish()
        print(json.dumps({"type": "error", "message": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
