# azd-test-tool

MCP server for Copilot CLI-driven testing of interactive CLI flows.

Instead of writing rigid test scripts with exact keystrokes, you write **goal-based scenarios** and let Copilot CLI figure out how to drive the terminal — just like Playwright MCP for browsers.

## How it works

```
┌─────────────┐     MCP (stdio)     ┌──────────────────┐     tmux     ┌─────────┐
│ Copilot CLI  │ ◄─────────────────► │  MCP Server      │ ◄──────────► │  CLI    │
│ (reads YAML) │   tools/resources   │  (auto_test_tool) │  send-keys   │  (azd)  │
└─────────────┘                      └──────────────────┘  capture-pane └─────────┘
```

1. **MCP server** exposes terminal control tools over stdio
2. **Copilot CLI** reads a scenario file with high-level goals
3. **tmux** runs the CLI command in a detached terminal session
4. Copilot CLI **observes** the terminal state and **decides** what actions to take
5. **Screenshots** (SVG) and an **HTML report** are generated automatically

## Quick Start

### 1. Install prerequisites

```bash
brew install tmux          # terminal backend
```

### 2. Clone and set up the venv

```bash
git clone <this-repo>
cd azd-test-tool
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e .
```

> **Note**: If you don't have `uv`, use `python3.12 -m venv .venv && source .venv/bin/activate && pip install -e .`

### 3. Register the MCP server with Copilot CLI

Add this entry to your Copilot CLI MCP config at **`~/.copilot/mcp-config.json`** (create the file if it doesn't exist):

```json
{
  "mcpServers": {
    "azd-test-tool": {
      "type": "stdio",
      "command": "<FULL-PATH-TO-REPO>/.venv/bin/python",
      "args": ["-m", "auto_test_tool.mcp_server"],
      "cwd": "<FULL-PATH-TO-REPO>"
    }
  }
}
```

Replace `<FULL-PATH-TO-REPO>` with the absolute path where you cloned the repo (e.g., `/Users/you/working/azd-test-tool`).

> **Important**: Use the full path to the **venv Python** (`<repo>/.venv/bin/python`), not a system Python. This ensures the MCP and other dependencies are available.

### 4. Run a scenario

Open a **new** Copilot CLI session (so it picks up the config) and say:

```
Use the azd-test-tool to load the scenario at scenarios/smoke-test.yaml,
then start the session and accomplish the goals. Take screenshots at each step.
```

That's it. Copilot CLI will use `load_scenario` to read the goals, then call `start_session`, `observe`, `send_action`, and `finish_session` to drive the CLI.

### 5. Run a real test

```
Use the azd-test-tool to load the scenario at scenarios/init-template-python.yaml
and drive the azd ai agent init session. Take screenshots at each step and generate a report.
```

## Writing Scenarios

Scenarios are YAML files. You provide the command to run and **goals** describing what you want to happen — Copilot CLI figures out the keystrokes.

### Structured goals (list of steps)

Best when you need a specific sequence:

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

### Free-text goal

Best for exploratory or flexible flows:

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

### Scenario fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Scenario name (used in report titles) |
| `command` | yes | CLI command to run in tmux |
| `cwd` | no | Working directory (supports `~`, created if missing) |
| `env` | no | Extra environment variables |
| `goals` | one of goals/goal | List of step-by-step goal descriptions |
| `goal` | one of goals/goal | Single free-text goal description |

## MCP Tools Reference

These are the tools Copilot CLI will use automatically. You don't call them directly — they're documented here so you understand what's happening and can write better scenario goals.

### `load_scenario`

Read a scenario YAML file and get the goals/configuration.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | Path to YAML file (absolute or relative, supports `~`) |

### `start_session`

Launch a command in a tmux terminal.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | string | required | CLI command to run |
| `cwd` | string | `"."` | Working directory |
| `session_id` | string | `"default"` | Session identifier (for concurrent sessions) |
| `env` | object | `{}` | Extra environment variables |
| `output_dir` | string | `"screenshots"` | Where to store screenshots/report |

### `observe`

Read the current terminal text. Call after every action to see the result.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `session_id` | string | `"default"` | Which session to observe |

### `send_action`

Send keystrokes to the interactive CLI.

| Parameter | Type | Used with | Description |
|-----------|------|-----------|-------------|
| `action` | string | all | One of: `select`, `confirm`, `input`, `multi_select`, `wait` |
| `choice_index` | int | `select` | 0-based index of item to pick |
| `choice_text` | string | `select` | Text label to match (scrolls to find it) |
| `value` | bool | `confirm` | `true` for yes, `false` for no |
| `text` | string | `input` | Text to type (empty = accept default) |
| `toggle_indices` | list[int] | `multi_select` | 0-based indices to toggle |
| `seconds` | float | `wait` | How long to pause |
| `session_id` | string | all | Which session to act on |

### `screenshot`

Capture the terminal as an SVG file.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `label` | string | `""` | Label for the screenshot in the report |
| `session_id` | string | `"default"` | Which session to capture |

### `finish_session`

Kill the tmux session and generate an HTML report.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `session_id` | string | `"default"` | Which session to finish |

## Output

Each session generates a timestamped directory:

```
screenshots/
  agent_20240101_120000/
    step_000.svg      # Terminal screenshot at step 0
    step_000.txt      # Plain text capture
    step_001.svg
    ...
    final.svg         # Final terminal state
    result.json       # Structured results
    report.html       # HTML report with embedded screenshots
```

Open `report.html` in a browser to review the test run.

## Tips for writing good goals

- **Be specific about what text to look for**: "Wait for 'Next:' to appear" is better than "wait for it to finish"
- **Name the choices**: "Select 'Python' as the language" is better than "pick a language"
- **Say what to do with defaults**: "Accept the default value" or "Enter 'my-agent' as the name"
- **Describe error handling**: "If prompted about an existing manifest, confirm yes"
- **Take screenshots at key moments**: Include "take a screenshot" in your prompt to Copilot CLI

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "tmux is required" | `brew install tmux` |
| MCP server not found | Check the `command` path in mcp-config.json points to the venv Python |
| "No active session" | Call `start_session` before `observe`/`send_action` |
| Terminal shows old state | Call `observe` — it waits for content to change |
| Stuck on a prompt | The `send_action` wait type can pause; or try a `select` with `choice_index: 0` to accept the highlighted default |
