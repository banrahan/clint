#!/usr/bin/env python3
"""
Automated test runner for interactive CLI tools like `azd ai agent init`.

Uses tmux as the terminal backend and pexpect for keystroke automation.
Captures screenshots via `tmux capture-pane` + Rich SVG rendering.
Test flows are defined in YAML scenario files.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.text import Text

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

SESSION_PREFIX = "azd-auto-test"


@dataclass
class StepResult:
    """Result of executing a single scenario step."""

    step_index: int
    expect_pattern: str
    action: str
    matched_text: str = ""
    ansi_capture: str = ""
    svg_path: str = ""
    text_path: str = ""
    elapsed_seconds: float = 0.0
    success: bool = True
    error: str = ""


@dataclass
class ScenarioResult:
    """Result of executing a full scenario."""

    name: str
    command: str
    start_time: str = ""
    end_time: str = ""
    steps: list[StepResult] = field(default_factory=list)
    success: bool = True
    error: str = ""


def tmux_session_name() -> str:
    """Generate a unique tmux session name."""
    return f"{SESSION_PREFIX}-{os.getpid()}"


def tmux_is_installed() -> bool:
    """Check if tmux is available."""
    try:
        subprocess.run(
            ["tmux", "-V"], capture_output=True, check=True, timeout=5
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def tmux_create_session(
    session: str, command: str, cwd: str, env: dict[str, str], width: int = 120, height: int = 40
) -> None:
    """Create a detached tmux session running the given command."""
    full_env = os.environ.copy()
    full_env.update(env)
    # Force color output in the tmux session
    full_env.setdefault("FORCE_COLOR", "1")
    full_env.setdefault("TERM", "xterm-256color")

    cmd = [
        "tmux", "new-session",
        "-d",
        "-s", session,
        "-x", str(width),
        "-y", str(height),
    ]
    if cwd:
        cmd.extend(["-c", os.path.expanduser(cwd)])
    cmd.append(command)

    subprocess.run(cmd, env=full_env, check=True, timeout=10)


def tmux_kill_session(session: str) -> None:
    """Kill a tmux session if it exists."""
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True,
        timeout=5,
    )


def tmux_capture_pane(session: str, with_ansi: bool = True) -> str:
    """Capture the current tmux pane content."""
    cmd = ["tmux", "capture-pane", "-t", session, "-p"]
    if with_ansi:
        cmd.append("-e")  # include ANSI escape sequences
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    return result.stdout


def tmux_send_keys(session: str, keys: str) -> None:
    """Send keys to a tmux session."""
    subprocess.run(
        ["tmux", "send-keys", "-t", session, keys],
        check=True,
        timeout=5,
    )


def tmux_send_text(session: str, text: str) -> None:
    """Send literal text to a tmux session (no key interpretation)."""
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "-l", text],
        check=True,
        timeout=5,
    )


def wait_for_text(
    session: str, pattern: str, timeout: float = 30.0, poll_interval: float = 0.5
) -> tuple[bool, str]:
    """
    Poll tmux pane until pattern appears or timeout.
    Returns (found, captured_text).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        capture = tmux_capture_pane(session, with_ansi=False)
        if pattern.lower() in capture.lower():
            return True, capture
        time.sleep(poll_interval)
    # Return last capture even on timeout
    return False, tmux_capture_pane(session, with_ansi=False)


def render_ansi_to_svg(ansi_text: str, output_path: str, title: str = "") -> None:
    """Render ANSI text to an SVG file using Rich."""
    console = Console(record=True, width=120, force_terminal=True)
    text = Text.from_ansi(ansi_text)
    console.print(text)
    svg = console.export_svg(title=title or "Terminal Capture")
    Path(output_path).write_text(svg)


def save_text_capture(text: str, output_path: str) -> None:
    """Save plain text capture to a file."""
    # Strip ANSI codes for the text version
    clean = ANSI_RE.sub("", text)
    Path(output_path).write_text(clean)


def execute_action(session: str, step: dict) -> None:
    """Execute a prompt action (select, confirm, input, multi-select)."""
    action = step.get("action", "")

    if action == "select":
        # Navigate to the right choice using arrow keys
        choice_index = step.get("choice_index")
        if choice_index is not None:
            for _ in range(choice_index):
                tmux_send_keys(session, "Down")
                time.sleep(0.1)
        elif "choice" in step:
            # Try to find the choice by text — send Down until we see it
            # highlighted, with a reasonable cap
            target = step["choice"]
            for _ in range(20):
                capture = tmux_capture_pane(session, with_ansi=False)
                # Check if our target is near a selection indicator
                lines = capture.split("\n")
                for line in lines:
                    if target.lower() in line.lower() and (">" in line or "❯" in line):
                        break
                else:
                    tmux_send_keys(session, "Down")
                    time.sleep(0.15)
                    continue
                break
        tmux_send_keys(session, "Enter")

    elif action == "confirm":
        value = step.get("value", True)
        tmux_send_keys(session, "y" if value else "n")
        tmux_send_keys(session, "Enter")

    elif action == "input":
        text = step.get("text", "")
        tmux_send_text(session, text)
        tmux_send_keys(session, "Enter")

    elif action == "multi_select":
        # Toggle specified items then confirm
        indices = step.get("toggle_indices", [0])
        current = 0
        for idx in sorted(indices):
            while current < idx:
                tmux_send_keys(session, "Down")
                time.sleep(0.1)
                current += 1
            tmux_send_keys(session, " ")  # space to toggle
            time.sleep(0.1)
        tmux_send_keys(session, "Enter")

    elif action == "wait":
        # Just wait, no action needed
        pass

    else:
        raise ValueError(f"Unknown action: {action}")


def run_scenario(scenario_path: str, output_dir: str) -> ScenarioResult:
    """Run a single test scenario from a YAML file."""
    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)

    name = scenario["name"]
    command = scenario["command"]
    cwd = scenario.get("cwd", ".")
    env = scenario.get("env", {})
    steps = scenario.get("steps", [])
    step_timeout = scenario.get("step_timeout", 30)

    # Ensure output directory exists
    run_dir = os.path.join(output_dir, f"{name}_{datetime.now():%Y%m%d_%H%M%S}")
    os.makedirs(run_dir, exist_ok=True)

    result = ScenarioResult(
        name=name,
        command=command,
        start_time=datetime.now().isoformat(),
    )

    session = tmux_session_name()

    try:
        # Ensure cwd exists
        cwd_expanded = os.path.expanduser(cwd)
        os.makedirs(cwd_expanded, exist_ok=True)

        # Create tmux session
        tmux_create_session(session, command, cwd, env)
        # Give the command a moment to start
        time.sleep(1)

        for i, step in enumerate(steps):
            step_result = StepResult(
                step_index=i,
                expect_pattern=step.get("expect", ""),
                action=step.get("action", "wait"),
            )
            start = time.time()

            try:
                # Wait for the expected prompt text
                if step_result.expect_pattern:
                    found, capture = wait_for_text(
                        session, step_result.expect_pattern, timeout=step_timeout
                    )
                    step_result.matched_text = capture
                    if not found:
                        step_result.success = False
                        step_result.error = (
                            f"Timeout waiting for: {step_result.expect_pattern}"
                        )
                        result.steps.append(step_result)
                        result.success = False
                        result.error = step_result.error
                        break

                # Capture screenshot before action
                if step.get("screenshot", True):
                    ansi_capture = tmux_capture_pane(session, with_ansi=True)
                    step_result.ansi_capture = ansi_capture

                    svg_file = os.path.join(run_dir, f"step_{i:03d}.svg")
                    render_ansi_to_svg(
                        ansi_capture, svg_file,
                        title=f"Step {i}: {step_result.expect_pattern[:60]}",
                    )
                    step_result.svg_path = svg_file

                    txt_file = os.path.join(run_dir, f"step_{i:03d}.txt")
                    save_text_capture(ansi_capture, txt_file)
                    step_result.text_path = txt_file

                # Execute the action
                execute_action(session, step)

                # Small delay after action for the UI to update
                time.sleep(step.get("delay_after", 0.5))

            except Exception as e:
                step_result.success = False
                step_result.error = str(e)
                result.success = False
                result.error = str(e)

            step_result.elapsed_seconds = time.time() - start
            result.steps.append(step_result)

            if not step_result.success:
                break

        # Final capture after all steps
        time.sleep(1)
        final_capture = tmux_capture_pane(session, with_ansi=True)
        final_svg = os.path.join(run_dir, "final.svg")
        render_ansi_to_svg(final_capture, final_svg, title="Final State")
        final_txt = os.path.join(run_dir, "final.txt")
        save_text_capture(final_capture, final_txt)

    finally:
        tmux_kill_session(session)

    result.end_time = datetime.now().isoformat()

    # Save result JSON
    result_json = os.path.join(run_dir, "result.json")
    with open(result_json, "w") as f:
        json.dump(
            {
                "name": result.name,
                "command": result.command,
                "start_time": result.start_time,
                "end_time": result.end_time,
                "success": result.success,
                "error": result.error,
                "steps": [
                    {
                        "step_index": s.step_index,
                        "expect_pattern": s.expect_pattern,
                        "action": s.action,
                        "success": s.success,
                        "error": s.error,
                        "elapsed_seconds": round(s.elapsed_seconds, 2),
                        "svg_path": os.path.basename(s.svg_path) if s.svg_path else "",
                        "text_path": os.path.basename(s.text_path) if s.text_path else "",
                    }
                    for s in result.steps
                ],
            },
            f,
            indent=2,
        )

    # Generate HTML report
    generate_html_report(run_dir, result)

    return result


def generate_html_report(run_dir: str, result: ScenarioResult) -> None:
    """Generate an HTML report with embedded SVG screenshots."""
    html_parts = [
        "<!DOCTYPE html><html><head>",
        "<meta charset='utf-8'>",
        f"<title>Test: {result.name}</title>",
        "<style>",
        "body { font-family: -apple-system, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }",
        ".step { margin: 20px 0; border: 1px solid #ddd; border-radius: 8px; padding: 16px; }",
        ".step.fail { border-color: #e74c3c; }",
        ".step-header { display: flex; justify-content: space-between; margin-bottom: 8px; }",
        ".badge { padding: 2px 8px; border-radius: 4px; font-size: 12px; }",
        ".badge-pass { background: #2ecc71; color: white; }",
        ".badge-fail { background: #e74c3c; color: white; }",
        "img, object { max-width: 100%; border: 1px solid #eee; border-radius: 4px; }",
        "pre { background: #f5f5f5; padding: 12px; border-radius: 4px; overflow-x: auto; }",
        "</style></head><body>",
        f"<h1>🧪 {result.name}</h1>",
        f"<p><code>{result.command}</code></p>",
        f"<p>{'✅ Passed' if result.success else '❌ Failed: ' + result.error}</p>",
        f"<p>{result.start_time} → {result.end_time}</p>",
    ]

    for step in result.steps:
        fail_class = "" if step.success else " fail"
        badge = "badge-pass" if step.success else "badge-fail"
        badge_text = "PASS" if step.success else "FAIL"

        html_parts.append(f'<div class="step{fail_class}">')
        html_parts.append('<div class="step-header">')
        html_parts.append(
            f"<strong>Step {step.step_index}: expect \"{step.expect_pattern}\" → {step.action}</strong>"
        )
        html_parts.append(
            f'<span class="badge {badge}">{badge_text} ({step.elapsed_seconds:.1f}s)</span>'
        )
        html_parts.append("</div>")

        if step.error:
            html_parts.append(f"<p style='color: #e74c3c;'>Error: {step.error}</p>")

        if step.svg_path and os.path.exists(step.svg_path):
            svg_name = os.path.basename(step.svg_path)
            html_parts.append(f'<object data="{svg_name}" type="image/svg+xml" width="100%"></object>')

        html_parts.append("</div>")

    # Final capture
    final_svg = os.path.join(run_dir, "final.svg")
    if os.path.exists(final_svg):
        html_parts.append('<div class="step"><strong>Final State</strong>')
        html_parts.append('<object data="final.svg" type="image/svg+xml" width="100%"></object>')
        html_parts.append("</div>")

    html_parts.append("</body></html>")

    report_path = os.path.join(run_dir, "report.html")
    Path(report_path).write_text("\n".join(html_parts))
    print(f"📄 Report: {report_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Automated test runner for interactive CLI tools"
    )
    parser.add_argument(
        "scenario",
        help="Path to a YAML scenario file or directory of scenarios",
    )
    parser.add_argument(
        "-o", "--output",
        default="screenshots",
        help="Output directory for screenshots and reports (default: screenshots/)",
    )
    args = parser.parse_args()

    if not tmux_is_installed():
        print("❌ tmux is required. Install with: brew install tmux", file=sys.stderr)
        sys.exit(1)

    scenario_path = Path(args.scenario)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if scenario_path.is_dir():
        scenarios = sorted(scenario_path.glob("*.yaml"))
    else:
        scenarios = [scenario_path]

    if not scenarios:
        print(f"❌ No scenario files found at: {scenario_path}", file=sys.stderr)
        sys.exit(1)

    results = []
    for scenario_file in scenarios:
        print(f"\n🚀 Running scenario: {scenario_file.name}")
        result = run_scenario(str(scenario_file), str(output_dir))
        results.append(result)
        status = "✅ PASS" if result.success else "❌ FAIL"
        print(f"   {status}: {result.name}")
        if result.error:
            print(f"   Error: {result.error}")

    # Summary
    passed = sum(1 for r in results if r.success)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} passed")


if __name__ == "__main__":
    main()
