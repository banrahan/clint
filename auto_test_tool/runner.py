#!/usr/bin/env python3
"""
Automated test runner for interactive CLI tools like `azd ai agent init`.

Uses tmux as the terminal backend and pexpect for keystroke automation.
Captures screenshots via `tmux capture-pane` + Rich SVG rendering.
Test flows are defined in YAML scenario files.
"""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
    label: str = ""
    timestamp: str = ""


@dataclass
class BugReport:
    """A bug or issue found during the session."""

    step_index: int
    title: str
    description: str = ""
    severity: str = "medium"  # low, medium, high, critical
    screenshot_path: str = ""


@dataclass
class ScenarioResult:
    """Result of executing a full scenario."""

    name: str
    command: str
    start_time: str = ""
    end_time: str = ""
    steps: list[StepResult] = field(default_factory=list)
    bugs: list[BugReport] = field(default_factory=list)
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


def tmux_session_alive(session: str) -> bool:
    """Check if a tmux session's pane process is still running."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, timeout=5,
        )
        # pane_dead is "1" when the command has exited
        return result.returncode == 0 and result.stdout.strip() != "1"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def tmux_capture_pane(session: str, with_ansi: bool = True) -> str:
    """Capture the current tmux pane content.

    If the visible pane is empty (e.g. the process exited), falls back to
    capturing the full scrollback history so the final output is preserved.
    """
    cmd = ["tmux", "capture-pane", "-t", session, "-p"]
    if with_ansi:
        cmd.append("-e")  # include ANSI escape sequences
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    text = result.stdout

    # If the visible pane is empty, grab the full scrollback
    if not text.strip():
        cmd_full = ["tmux", "capture-pane", "-t", session, "-p", "-S", "-"]
        if with_ansi:
            cmd_full.append("-e")
        result_full = subprocess.run(cmd_full, capture_output=True, text=True, timeout=5)
        if result_full.stdout.strip():
            text = result_full.stdout

    return text


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
    console = Console(record=True, width=120, force_terminal=True, file=open(os.devnull, "w"))
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


def generate_html_report(run_dir: str, result: ScenarioResult) -> None:
    """Generate an HTML report with timeline, bugs section, and embedded SVG screenshots."""

    total_time = sum(s.elapsed_seconds for s in result.steps)
    failed_steps = [s for s in result.steps if not s.success]
    bug_count = len(result.bugs)

    # Severity colors
    sev_colors = {
        "critical": "#dc2626",
        "high": "#ea580c",
        "medium": "#d97706",
        "low": "#65a30d",
    }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Test Report: {result.name}</title>
<style>
:root {{
  --bg: #1a1a2e;
  --surface: #16213e;
  --surface2: #0f3460;
  --text: #e6e6e6;
  --text-muted: #94a3b8;
  --accent: #e94560;
  --green: #22c55e;
  --red: #ef4444;
  --yellow: #eab308;
  --blue: #3b82f6;
  --border: #334155;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
}}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}

/* Header */
.header {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.header h1 {{ font-size: 1.5rem; margin-bottom: 8px; }}
.header .command {{ color: var(--text-muted); font-family: monospace; font-size: 0.9rem; }}
.stats {{
  display: flex;
  gap: 24px;
  margin-top: 16px;
  flex-wrap: wrap;
}}
.stat {{
  background: var(--bg);
  border-radius: 8px;
  padding: 12px 20px;
  text-align: center;
  min-width: 100px;
}}
.stat-value {{ font-size: 1.5rem; font-weight: 700; }}
.stat-label {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }}
.stat-pass .stat-value {{ color: var(--green); }}
.stat-fail .stat-value {{ color: var(--red); }}
.stat-bug .stat-value {{ color: var(--yellow); }}

/* Timeline */
.timeline-section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.timeline-section h2 {{ font-size: 1.1rem; margin-bottom: 16px; }}
.timeline-bar {{
  display: flex;
  height: 32px;
  border-radius: 6px;
  overflow: hidden;
  background: var(--bg);
  margin-bottom: 8px;
}}
.timeline-bar .segment {{
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.65rem;
  color: white;
  cursor: pointer;
  transition: opacity 0.2s;
  min-width: 2px;
  border-right: 1px solid var(--bg);
}}
.timeline-bar .segment:hover {{ opacity: 0.8; }}
.timeline-bar .segment.pass {{ background: var(--green); }}
.timeline-bar .segment.fail {{ background: var(--red); }}
.timeline-legend {{
  display: flex;
  gap: 16px;
  font-size: 0.75rem;
  color: var(--text-muted);
}}
.timeline-legend span::before {{
  content: '';
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 4px;
  vertical-align: middle;
}}
.timeline-legend .leg-pass::before {{ background: var(--green); }}
.timeline-legend .leg-fail::before {{ background: var(--red); }}

/* Bugs Section */
.bugs-section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.bugs-section h2 {{ font-size: 1.1rem; margin-bottom: 16px; }}
.bug-card {{
  background: var(--bg);
  border-left: 4px solid var(--yellow);
  border-radius: 0 8px 8px 0;
  padding: 16px;
  margin-bottom: 12px;
}}
.bug-card .bug-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}}
.bug-card .bug-title {{ font-weight: 600; }}
.severity-badge {{
  padding: 2px 10px;
  border-radius: 12px;
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: white;
}}
.bug-card .bug-desc {{ color: var(--text-muted); font-size: 0.9rem; }}
.bug-card .bug-step {{ color: var(--text-muted); font-size: 0.75rem; margin-top: 4px; }}
.no-bugs {{
  text-align: center;
  padding: 32px;
  color: var(--green);
  font-size: 1.1rem;
}}

/* Steps */
.steps-section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.steps-section h2 {{ font-size: 1.1rem; margin-bottom: 16px; }}
.step {{
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 8px;
  overflow: hidden;
}}
.step.fail {{ border-color: var(--red); }}
.step-summary {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
}}
.step-summary:hover {{ background: var(--surface2); }}
.step-summary .step-left {{
  display: flex;
  align-items: center;
  gap: 10px;
}}
.step-summary .step-icon {{ font-size: 0.9rem; }}
.step-summary .step-label {{ font-size: 0.9rem; }}
.step-summary .step-right {{
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 0.8rem;
  color: var(--text-muted);
}}
.step-details {{
  display: none;
  padding: 16px;
  border-top: 1px solid var(--border);
}}
.step.open .step-details {{ display: block; }}
.step-details pre {{
  background: var(--surface);
  padding: 12px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 0.8rem;
  margin-top: 8px;
}}
.step-details .error-msg {{
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid var(--red);
  color: var(--red);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 0.85rem;
  margin-bottom: 8px;
}}
.step-details object, .step-details img {{
  max-width: 100%;
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-top: 8px;
}}
.screenshot svg {{
  max-width: 100%;
  height: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-top: 8px;
}}

/* Final capture */
.final-section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.final-section h2 {{ font-size: 1.1rem; margin-bottom: 16px; }}
.final-section object {{ max-width: 100%; border: 1px solid var(--border); border-radius: 6px; }}
</style>
</head>
<body>
<div class="container">
"""

    # --- Header ---
    status_emoji = "✅" if result.success else "❌"
    status_text = "Passed" if result.success else f"Failed: {result.error}"
    html += f"""
<div class="header">
  <h1>{status_emoji} {result.name}</h1>
  <div class="command">{result.command}</div>
  <div class="stats">
    <div class="stat stat-pass">
      <div class="stat-value">{len(result.steps) - len(failed_steps)}</div>
      <div class="stat-label">Passed</div>
    </div>
    <div class="stat stat-fail">
      <div class="stat-value">{len(failed_steps)}</div>
      <div class="stat-label">Failed</div>
    </div>
    <div class="stat stat-bug">
      <div class="stat-value">{bug_count}</div>
      <div class="stat-label">Bugs</div>
    </div>
    <div class="stat">
      <div class="stat-value">{total_time:.1f}s</div>
      <div class="stat-label">Duration</div>
    </div>
    <div class="stat">
      <div class="stat-value">{len(result.steps)}</div>
      <div class="stat-label">Steps</div>
    </div>
  </div>
  <div style="margin-top: 12px; font-size: 0.8rem; color: var(--text-muted);">
    {result.start_time} → {result.end_time}
  </div>
</div>
"""

    # --- Timeline ---
    html += '<div class="timeline-section"><h2>⏱ Timeline</h2>\n<div class="timeline-bar">\n'
    if total_time > 0:
        for step in result.steps:
            pct = max((step.elapsed_seconds / total_time) * 100, 0.5)
            cls = "pass" if step.success else "fail"
            label = step.label or f"Step {step.step_index}"
            # Truncate long labels for the bar
            short_label = label[:20] if len(label) > 20 else label
            html += (
                f'  <div class="segment {cls}" style="width:{pct:.1f}%" '
                f'title="{label} ({step.elapsed_seconds:.1f}s)" '
                f'onclick="toggleStep({step.step_index})">'
                f'{short_label if pct > 8 else ""}</div>\n'
            )
    html += '</div>\n'
    html += '<div class="timeline-legend">'
    html += '<span class="leg-pass">Pass</span>'
    html += '<span class="leg-fail">Fail</span>'
    html += f'<span>Total: {total_time:.1f}s</span>'
    html += '</div>\n</div>\n'

    # --- Bugs Section ---
    html += '<div class="bugs-section"><h2>🐛 Bugs &amp; Issues</h2>\n'
    if result.bugs:
        for bug in result.bugs:
            sev_color = sev_colors.get(bug.severity, sev_colors["medium"])
            html += f"""
<div class="bug-card" style="border-left-color: {sev_color};">
  <div class="bug-header">
    <span class="bug-title">{bug.title}</span>
    <span class="severity-badge" style="background: {sev_color};">{bug.severity}</span>
  </div>
  <div class="bug-desc">{bug.description}</div>
  <div class="bug-step">Step {bug.step_index}</div>
"""
            if bug.screenshot_path and os.path.exists(bug.screenshot_path):
                bug_svg = Path(bug.screenshot_path).read_text()
                html += f'  <div class="screenshot">{bug_svg}</div>\n'
            html += '</div>\n'
    elif failed_steps:
        # Auto-generate bug entries from failed steps
        for step in failed_steps:
            html += f"""
<div class="bug-card">
  <div class="bug-header">
    <span class="bug-title">Step {step.step_index} failed</span>
    <span class="severity-badge" style="background: {sev_colors['high']};">high</span>
  </div>
  <div class="bug-desc">{step.error or 'Step did not complete successfully.'}</div>
  <div class="bug-step">Action: {step.action}</div>
</div>
"""
    else:
        html += '<div class="no-bugs">✅ No bugs found</div>\n'
    html += '</div>\n'

    # --- Steps ---
    html += '<div class="steps-section"><h2>📋 Steps</h2>\n'
    for step in result.steps:
        fail_cls = " fail" if not step.success else ""
        icon = "❌" if not step.success else "✅"
        label = step.label or f"expect \"{step.expect_pattern}\" → {step.action}"
        time_str = f"{step.elapsed_seconds:.1f}s"
        timestamp_str = f" at {step.timestamp}" if step.timestamp else ""

        html += f"""
<div class="step{fail_cls}" id="step-{step.step_index}">
  <div class="step-summary" onclick="this.parentElement.classList.toggle('open')">
    <div class="step-left">
      <span class="step-icon">{icon}</span>
      <span class="step-label"><strong>Step {step.step_index}</strong> — {label}</span>
    </div>
    <div class="step-right">
      <span>{time_str}</span>
    </div>
  </div>
  <div class="step-details">
"""
        if step.error:
            html += f'    <div class="error-msg">⚠️ {step.error}</div>\n'
        html += f'    <pre>Action: {step.action}{timestamp_str}</pre>\n'
        if step.svg_path and os.path.exists(step.svg_path):
            svg_content = Path(step.svg_path).read_text()
            html += f'    <div class="screenshot">{svg_content}</div>\n'
        html += '  </div>\n</div>\n'

    html += '</div>\n'

    # --- Final capture ---
    final_svg = os.path.join(run_dir, "final.svg")
    if os.path.exists(final_svg):
        final_svg_content = Path(final_svg).read_text()
        html += f"""
<div class="final-section">
  <h2>🏁 Final State</h2>
  <div class="screenshot">{final_svg_content}</div>
</div>
"""

    # --- Script for interactivity ---
    html += """
<script>
function toggleStep(index) {
  const el = document.getElementById('step-' + index);
  if (el) {
    el.classList.toggle('open');
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}
// Auto-expand failed steps
document.querySelectorAll('.step.fail').forEach(el => el.classList.add('open'));
</script>
"""

    html += '</div></body></html>'

    report_path = os.path.join(run_dir, "report.html")
    Path(report_path).write_text(html)
    print(f"📄 Report: {report_path}", file=sys.stderr)


