# azd auto-test-tool

Automated testing tool for interactive CLI flows like `azd ai agent init`.

## How it works

1. **tmux** runs the CLI command in a detached terminal session with controlled dimensions
2. **pexpect-style polling** waits for prompt text to appear
3. **Keystrokes** are sent via `tmux send-keys` (arrow keys, enter, text input)
4. **Screenshots** are captured via `tmux capture-pane -p -e` (ANSI text) → rendered to SVG using Rich
5. **HTML report** is generated with embedded SVG screenshots at each step

## Prerequisites

```bash
brew install tmux
pip install -e .
```

## Usage

```bash
# Run a single scenario
azd-auto-test scenarios/init-template-python.yaml

# Run all scenarios in a directory
azd-auto-test scenarios/

# Custom output directory
azd-auto-test scenarios/init-template-python.yaml -o ~/Desktop/test-results
```

## Writing scenarios

Scenarios are YAML files with this structure:

```yaml
name: "my-test"
command: "azd ai agent init"
cwd: "~/working/agents/test-dir"
step_timeout: 30  # seconds per step
env:
  AZD_DISABLE_AGENT_DETECT: "1"
steps:
  - expect: "text to wait for"
    action: select          # select | confirm | input | multi_select | wait
    choice: "option text"   # for select: match by text
    choice_index: 0         # for select: match by position
    value: true             # for confirm: true/false
    text: "my input"        # for input: text to type
    toggle_indices: [0, 2]  # for multi_select: items to toggle
    screenshot: true        # capture before action (default: true)
    delay_after: 0.5        # seconds to wait after action
```

### Action types

| Action | Description | Key fields |
|--------|------------|------------|
| `select` | Arrow-key navigation + Enter | `choice` or `choice_index` |
| `confirm` | Y/N + Enter | `value` (bool) |
| `input` | Type text + Enter | `text` |
| `multi_select` | Space to toggle + Enter | `toggle_indices` |
| `wait` | Just wait, no keystroke | — |

## Output

Each run creates a timestamped directory under `screenshots/`:

```
screenshots/
  init-from-template-python_20240101_120000/
    step_000.svg      # Terminal screenshot at step 0
    step_000.txt      # Plain text capture
    step_001.svg
    step_001.txt
    final.svg         # Final terminal state
    result.json       # Structured results
    report.html       # HTML report with embedded screenshots
```

Open `report.html` in a browser to review the test run.

## Agent-driven exploration (Copilot CLI)

The agent mode lets **Copilot CLI** (or any external agent) drive the
interactive CLI. No API keys needed — you are the agent.

### How it works

`azd-auto-agent` uses a JSON-over-stdio protocol:
1. It starts the command in tmux and prints the terminal state as JSON to stdout
2. You (Copilot CLI) read the state, decide what to do, and send a JSON action to stdin
3. The tool executes the action, captures a screenshot, prints the new state
4. Repeat until you send `{"action": "done"}`

### Usage from Copilot CLI

```bash
# Start an agent session — Copilot CLI drives it interactively
azd-auto-agent "azd ai agent init" -d ~/working/agents/test -o screenshots
```

The tool prints JSON lines. Send JSON actions:

```json
{"action": "select", "choice_index": 1}
{"action": "select_by_text", "text": "Python"}
{"action": "confirm", "value": true}
{"action": "input", "text": "my-agent"}
{"action": "multi_select", "toggle_indices": [0, 2]}
{"action": "wait", "seconds": 3}
{"action": "done", "summary": "Explored template init flow"}
```

### Python API

```python
from auto_test_tool.agent import AgentSession

session = AgentSession("azd ai agent init", cwd="~/agents/test")
state = session.start()   # returns terminal text

# Drive the session
state = session.act({"action": "select", "choice_index": 0})
state = session.act({"action": "select_by_text", "text": "Python"})
# ...

report = session.finish()  # generates HTML report
```
