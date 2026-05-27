# azd-test-tool

MCP server for Copilot CLI-driven testing of interactive CLI flows.

Instead of writing rigid test scripts with exact keystrokes, you write **goal-based scenarios** and let Copilot CLI figure out how to drive the terminal — just like Playwright MCP for browsers.

## How it works

1. **MCP server** exposes terminal control tools over stdio
2. **Copilot CLI** reads a scenario file with high-level goals
3. **tmux** runs the CLI command in a detached terminal session
4. Copilot CLI **observes** the terminal state and **decides** what actions to take
5. **Screenshots** (SVG) and an **HTML report** are generated automatically

## Prerequisites

```bash
brew install tmux
uv pip install -e .
# or: pip install -e .
```

## MCP Configuration

Add to your MCP config (e.g., `~/.config/github-copilot/mcp.json`):

```json
{
  "servers": {
    "azd-test-tool": {
      "command": "python",
      "args": ["-m", "auto_test_tool.mcp_server"],
      "cwd": "/path/to/azd-test-tool"
    }
  }
}
```

Or run directly:

```bash
azd-test-mcp
```

## Writing Scenarios

Scenarios are YAML files with goals instead of rigid steps.

### Structured goals (list of steps):

```yaml
name: "init-template-python"
command: "azd ai agent init"
cwd: "~/working/agents/test"
env:
  AZD_DISABLE_AGENT_DETECT: "1"
goals:
  - "Select 'Start new from a template' when asked how to initialize"
  - "Choose Python as the language"
  - "Pick the first starter template"
  - "Wait for init to complete (look for 'Next:' in output)"
```

### Free-text goal:

```yaml
name: "init-from-code"
command: "azd ai agent init"
cwd: "~/working/agents/existing-project"
goal: |
  Initialize from existing agent source code.
  Confirm reuse of the existing manifest.
  Accept defaults for all prompts.
  Wait for "Next:" completion message.
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `start_session` | Launch a command in tmux. Returns initial terminal state. |
| `observe` | Read current terminal text. |
| `send_action` | Send an action: `select`, `confirm`, `input`, `multi_select`, `wait`. |
| `screenshot` | Capture terminal → SVG file. |
| `finish_session` | Kill session, generate HTML report. |

### Action types for `send_action`

| Action | Parameters | Description |
|--------|-----------|-------------|
| `select` | `choice_index` or `choice_text` | Pick from a list |
| `confirm` | `value` (bool) | Answer yes/no |
| `input` | `text` | Type into a text field |
| `multi_select` | `toggle_indices` (list) | Toggle items in a checklist |
| `wait` | `seconds` | Pause without sending keys |

## MCP Resources

| Resource | Description |
|----------|-------------|
| `scenario://{path}` | Read a scenario YAML and return structured goals |

## Output

Each session generates a timestamped directory:

```
screenshots/
  agent_20240101_120000/
    step_000.svg      # Terminal screenshot
    step_000.txt      # Plain text capture
    final.svg         # Final terminal state
    result.json       # Structured results
    report.html       # HTML report with embedded screenshots
```

## Usage with Copilot CLI

Ask Copilot CLI to run a scenario:

> "Read the scenario at scenarios/init-template-python.yaml and drive the CLI session to accomplish those goals. Take screenshots at each step."

Copilot CLI will:
1. Read the scenario via the `scenario://` resource
2. Call `start_session` with the command and working directory
3. Call `observe` to see the terminal
4. Call `send_action` to interact with each prompt
5. Call `screenshot` to capture key moments
6. Call `finish_session` to generate the report
