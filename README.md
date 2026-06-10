# cli-interactive-tester

MCP server for Copilot CLI-driven testing of **any** interactive CLI flow.

Instead of writing rigid test scripts with exact keystrokes, you write **goal-based scenarios** and let Copilot CLI figure out how to drive the terminal — just like Playwright MCP for browsers.

## How it works

```
┌─────────────┐     MCP (stdio)     ┌──────────────────┐     tmux     ┌──────────┐
│ Copilot CLI  │ ◄─────────────────► │  MCP Server      │ ◄──────────► │ Any CLI  │
│ (reads YAML) │   tools/resources   │  (auto_test_tool) │  send-keys   │ (interactive)
└─────────────┘                      └──────────────────┘  capture-pane └──────────┘
```

1. **MCP server** exposes terminal control tools over stdio
2. **Copilot CLI** reads a scenario file with high-level goals
3. **tmux** runs the CLI command in a detached terminal session
4. Copilot CLI **observes** the terminal state and **decides** what actions to take
5. **Screenshots** (SVG) and an **HTML report** are generated automatically

## Quick Start

### 1. Install prerequisites

<details>
<summary><strong>macOS / Linux</strong></summary>

```bash
brew install tmux          # terminal backend (macOS)
# or: sudo apt install tmux  (Debian/Ubuntu)
```

</details>

<details>
<summary><strong>Windows</strong></summary>

The MCP server shells out to `tmux` directly, so on Windows **both tmux and the MCP server must run inside WSL** (Windows Subsystem for Linux). The Windows-side `python.exe` cannot find `tmux` and will fail with `ERROR: tmux is required`.

```powershell
wsl --install                       # if WSL isn't set up yet
wsl sudo apt update
wsl sudo apt install -y tmux
wsl tmux -V                         # verify, e.g. "tmux 3.6"
```

> **Note**: You will set up a separate Linux venv inside WSL in step 2, and register the MCP server to launch via `wsl` in step 3.

</details>

### 2. Clone and set up the venv

<details>
<summary><strong>macOS / Linux</strong></summary>

```bash
git clone https://github.com/coreai-microsoft/cli-interactive-tester.git
cd cli-interactive-tester
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e .
```

> **Note**: If you don't have `uv`, use `python3.12 -m venv .venv && source .venv/bin/activate && pip install -e .`

</details>

<details>
<summary><strong>Windows (PowerShell + WSL)</strong></summary>

Because the MCP server must run inside WSL (see prerequisites), create the venv **inside WSL**, not on the Windows side. You can keep the repo checked out on the Windows filesystem and access it from WSL via `/mnt/c/...`.

```powershell
git clone https://github.com/coreai-microsoft/cli-interactive-tester.git
cd cli-interactive-tester

# Install uv inside WSL (one-time)
wsl bash -lc "curl -LsSf https://astral.sh/uv/install.sh | sh"

# Create a Linux venv inside the repo and install the package editable
wsl bash -lc "cd /mnt/c/Repos/cli-interactive-tester && ~/.local/bin/uv venv .venv-wsl --python 3.12 && ~/.local/bin/uv pip install --python .venv-wsl/bin/python -e ."

# Sanity check
wsl /mnt/c/Repos/cli-interactive-tester/.venv-wsl/bin/python -c "import auto_test_tool.mcp_server; print('OK')"
```

> **Note**: If you don't want `uv`, substitute `python3 -m venv .venv-wsl && .venv-wsl/bin/pip install -e .` inside WSL.

> **Note**: Adjust `/mnt/c/Repos/cli-interactive-tester` to wherever you cloned the repo.

</details>

### 3. Register the MCP server with Copilot CLI

Add this entry to your Copilot CLI MCP config at **`~/.copilot/mcp-config.json`** (create the file if it doesn't exist):

<details>
<summary><strong>macOS / Linux</strong></summary>

```json
{
  "mcpServers": {
    "cli-interactive-tester": {
      "type": "stdio",
      "command": "<FULL-PATH-TO-REPO>/.venv/bin/python",
      "args": ["-m", "auto_test_tool.mcp_server"],
      "cwd": "<FULL-PATH-TO-REPO>"
    }
  }
}
```

Example path: `/Users/you/working/cli-interactive-tester`

</details>

<details>
<summary><strong>Windows</strong></summary>

On Windows the server is launched **through `wsl`** so it can reach `tmux`. Point `command` at `wsl` and use a bash login shell to `cd` into the repo (via its `/mnt/c/...` path) and exec the Linux venv's Python:

```json
{
  "mcpServers": {
    "cli-interactive-tester": {
      "type": "stdio",
      "command": "wsl",
      "args": [
        "bash",
        "-lc",
        "cd /mnt/c/Repos/cli-interactive-tester && exec .venv-wsl/bin/python -m auto_test_tool.mcp_server"
      ]
    }
  }
}
```

Replace `/mnt/c/Repos/cli-interactive-tester` with the WSL path to your clone (a Windows path like `C:\Repos\foo` becomes `/mnt/c/Repos/foo`).

</details>

> **Important**: On macOS/Linux, use the full path to the **venv Python**, not a system Python. On Windows, launch through `wsl` so `tmux` is on PATH; the Windows-side `python.exe` will fail with `ERROR: tmux is required`.

### 4. Run a scenario

Open a **new** Copilot CLI session (so it picks up the config) and say:

```
Use the cli-interactive-tester to load the scenario at scenarios/smoke-test.yaml,
then start the session and accomplish the goals. If the scenario declares pre or
post hooks, run them before/after the session. Take screenshots at each step.
```

That's it. Copilot CLI will use `load_scenario` to read the goals, then call `run_pre_hooks` (if any), `start_session`, `observe`, `send_action`, `finish_session`, and `run_post_hooks` (if any) to drive the CLI.

## Writing Scenarios

Scenarios are YAML files. You provide the command to run and **goals** describing what you want to happen — Copilot CLI figures out the keystrokes.

### Structured goals (list of steps)

Best when you need a specific sequence:

```yaml
name: "npm-init"
command: "npm init"
cwd: "~/working/my-project"
goals:
  - "Enter 'my-app' as the package name"
  - "Accept the default version"
  - "Enter 'A sample application' as the description"
  - "Wait for package.json to be created"
```

### Free-text goal

Best for exploratory or flexible flows:

```yaml
name: "interactive-setup"
command: "python setup_wizard.py"
cwd: "~/working/my-project"
goal: |
  Complete the interactive setup wizard.
  Select the recommended defaults for all prompts.
  When asked for a project name, enter "demo".
  Wait for the "Setup complete" message.
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
| `pre` | no | List of host shell hooks to run **before** `start_session` (see below) |
| `post` | no | List of host shell hooks to run **after** `finish_session` (see below) |
| `allocate_ports` | no | Reserve free TCP ports per scenario for `{name}` substitution (see "Parallelism & port allocation" below) |

### Parallelism & port allocation

CLIs that bind a fixed port (e.g. `azd ai agent run` defaults to `8088`)
can't be exercised in parallel — or paired with a `--local` invoker —
without colliding on the port. Declaring `allocate_ports:` in the
scenario reserves free OS-assigned ports per **scenario run** and
exposes them as `{name}` placeholders in `command`, `cwd`, `env`, hook
fields, and `goals` / `goal` strings.

Two forms:

```yaml
allocate_ports: [agent]        # named  → {agent}
# or
allocate_ports: 2              # numbered → {port1}, {port2}; {port} aliases {port1}
```

A pool is **shared across every `start_session` call that passes the
same `scenario_path`**, so two sessions of one scenario see the same
`{agent}` value — letting the `run` session and the `invoke --local`
session find each other. The pool is released automatically when the
last session for the scenario finishes.

Example: parallel `azd ai agent run` + `invoke --local`:

```yaml
name: "parallel-agent"
allocate_ports: [agent]
command: "azd ai agent run --port {agent} --no-inspector"
cwd: "/tmp/parallel-agent"

goals:
  - "Start the agent. Open a second session (session_id='invoke') with command:
     azd ai agent invoke --local --port {agent} 'Hi'
     Confirm both sessions report port {agent}."
```

Copilot CLI will:

1. `load_scenario(...)` — sees `Allocated ports: agent=49733` and the
   goal text with `{agent}` already replaced.
2. `start_session(scenario_path=..., command="azd ai agent run --port {agent} --no-inspector", session_id="run")`
   — substitution + tmux launch.
3. `start_session(scenario_path=..., command="azd ai agent invoke --local --port {agent} 'Hi'", session_id="invoke")`
   — same `scenario_path` → same pool → same port.

#### Per-call `session_vars`

`start_session(..., session_vars={"name": "demo"})` adds extra
`{name}` substitutions for that one call (useful when you don't have a
scenario file at all). They take precedence over allocated ports if
names collide.

#### Running N parallel instances of one scenario

To run the **same scenario** multiple times concurrently (e.g. a stress
test, or fanning out N independent agents), pass a distinct
`instance_id` per call. Each `instance_id` gets its own port pool, and
the value is exposed to substitution as `{instance}`:

```yaml
name: "parallel-instances"
allocate_ports: [agent]
cwd: "/tmp/agent-{instance}"
command: "azd ai agent run --port {agent} --no-inspector"
```

```text
start_session(scenario_path=..., instance_id="1", session_id="run-1", ...)
start_session(scenario_path=..., instance_id="2", session_id="run-2", ...)
start_session(scenario_path=..., instance_id="3", session_id="run-3", ...)
```

Each call materialises a separate pool (`{path}#1`, `{path}#2`, ...),
allocates an independent `{agent}` port, and resolves `{instance}` to
`"1"`, `"2"`, `"3"`. Within one `instance_id`, subsequent
`start_session` calls (e.g. paired `run` + `invoke --local`) share that
instance's pool. When `instance_id` is omitted, `{instance}` defaults
to `"main"` so single-instance scenarios keep working unchanged.

### Pre and post hooks

`pre` and `post` declare host shell commands (run outside tmux) for setup and
cleanup — e.g., create a working directory, `git init`, seed fixture files,
then tear them down after the run. Copilot CLI invokes them via the
`run_pre_hooks` and `run_post_hooks` MCP tools.

Each entry is either a **string** (the command) or a **mapping** with:

| Field | Default | Description |
|-------|---------|-------------|
| `run` | required | Shell command (executed via `bash -c` semantics, so pipes/`&&` work) |
| `cwd` | scenario `cwd` | Working directory; created if missing |
| `env` | `{}` | Extra environment variables merged onto the inherited environment |
| `continue_on_error` | `false` | If `true`, a non-zero exit does **not** abort the remaining hooks |
| `timeout` | `120` | Per-hook timeout in seconds |
| `name` | `run` value | Label shown in the result summary |

Execution is **sequential and fail-fast**: the first failing hook aborts the
phase unless that hook opts in to `continue_on_error: true`. Subsequent hooks
after an abort are reported as `SKIPPED`.

```yaml
name: "with-setup"
command: "my-cli init"
cwd: "/tmp/with-setup"

pre:
  - "rm -rf /tmp/with-setup"
  - run: "mkdir -p /tmp/with-setup"
    name: "create working dir"
  - run: "echo 'hello world' > hello.txt"
    cwd: "/tmp/with-setup"

goals:
  - "Accept the default project name"
  - "Wait for 'Done'"

post:
  - run: "rm -rf /tmp/with-setup"
    continue_on_error: true
```

> **Note (Windows / WSL)**: The MCP server runs inside WSL on Windows, so
> `cwd` and `run` values are interpreted as Linux paths (e.g.,
> `/mnt/c/Repos/foo`). Scenario YAMLs are trusted input — they execute
> arbitrary shell commands on the host.

## MCP Tools Reference

These are the tools Copilot CLI will use automatically. You don't call them directly — they're documented here so you understand what's happening and can write better scenario goals.

### `load_scenario`

Read a scenario YAML file and get the goals/configuration. Surfaces any
declared pre/post hooks so you know to invoke `run_pre_hooks` /
`run_post_hooks`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | Path to YAML file (absolute or relative, supports `~`) |

### `run_pre_hooks`

Execute the scenario's `pre:` list of host shell hooks before launching the
CLI. Fail-fast unless a hook sets `continue_on_error: true`. See the
"Pre and post hooks" section above for the schema.

| Parameter | Type | Description |
|-----------|------|-------------|
| `scenario_path` | string | Path to the YAML scenario file |

### `run_post_hooks`

Execute the scenario's `post:` list of host shell hooks after the session ends
(typically cleanup). Same semantics as `run_pre_hooks`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `scenario_path` | string | Path to the YAML scenario file |

### `start_session`

Launch a command in a tmux terminal. `command`, `cwd`, and `env` values
may contain `{name}` placeholders that resolve from the scenario's
allocated ports (see "Parallelism & port allocation") and `session_vars`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | string | required | CLI command to run |
| `cwd` | string | `null` | Working directory (auto temp-dir when omitted) |
| `session_id` | string | `"default"` | Session identifier (for concurrent sessions) |
| `env` | object | `{}` | Extra environment variables |
| `output_dir` | string | `"reports"` | Where to store screenshots/report |
| `run_name` | string | timestamp | Folder name for this run |
| `scenario_path` | string | `null` | Share the scenario's port pool with other sessions of the same scenario |
| `session_vars` | object | `{}` | Extra `{name}` substitutions for this call |

### `release_scenario_ports`

Drop the shared port pool for a scenario. `finish_session` releases it
automatically when the last session for the scenario tears down — call
this only on error paths where you abandoned a scenario without
finishing every session.

| Parameter | Type | Description |
|-----------|------|-------------|
| `scenario_path` | string | Path passed to `start_session` / `load_scenario` earlier |

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
| `choice_text` | string | `select` | Text label to match — see "How `choice_text` resolves" below |
| `value` | bool | `confirm` | `true` for yes, `false` for no |
| `text` | string | `input` | Text to type (empty = accept default) |
| `toggle_indices` | list[int] | `multi_select` | 0-based indices to toggle |
| `seconds` | float | `wait` | How long to pause |
| `session_id` | string | all | Which session to act on |

#### How `choice_text` resolves

For Survey-style pickers (the kind that show `Filter: Type to filter list`),
`select(choice_text="...")` runs a three-phase lookup:

1. **Already highlighted?** Capture the pane; if the highlighted line
   (the one with `>` or `❯`) already contains the target as a
   case-insensitive substring, press Enter and return.
2. **Filter typing.** Otherwise type the target text into the picker's
   filter, wait briefly, and Enter if the highlight now matches.
3. **Scroll fallback.** If filter typing didn't land (older non-filterable
   pickers, multi-line label, etc.), clear the filter with Backspaces and
   arrow-key scroll, Entering on the first highlighted match.

If none of the three phases finds a match, **`select` raises a
`LookupError`**. The MCP `send_action` tool surfaces that error in the
response body (prefixed with `ERROR during 'select': …`) so the agent
sees the failure instead of a silent wrong-pick.

> **Note for test runs.** Don't `observe` after every `select` to
> "verify" the pick — these scenarios exist to *test* the CLI under
> drive, and reactive verification masks the very bugs (silent mispicks,
> picker regressions) the run is meant to catch. Send the action and
> let downstream prompts surface any failure. The `LookupError` /
> `ERROR during 'select': …` surface covers the "target not in list"
> case — treat it as a hard failure, not a retry signal.

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

## Running in CI (record-and-replay)

The interactive flow above needs a human + Copilot CLI in the loop. To
gate PRs, the repo also supports **deterministic record-and-replay**:

1. Author a scenario as usual (`scenarios/<name>.yaml`).
2. Drive it once interactively with the new `record_plan` MCP tool —
   the session emits a self-contained YAML "test plan" under
   `plans/<name>.plan.yaml`.
3. Review/trim the plan and commit it. CI re-executes it deterministically
   on every PR using `cli-tester-replay` — no LLM, no API keys, no flake.

### Recording a plan

From an interactive Copilot CLI session (after the MCP server is
registered and restarted to pick up new tools):

```
Use cli-interactive-tester. Call record_plan with
  scenario_path='scenarios/smoke-test.yaml'
then drive scenarios/smoke-test.yaml end-to-end with start_session,
observe, send_action, screenshot, finish_session. The plan will be
written to plans/smoke-test.plan.yaml automatically.
```

The `record_plan` tool accepts:

| Parameter | Description |
|-----------|-------------|
| `plan_path` | Where to write the plan. Defaults to `plans/<scenario-stem>.plan.yaml`. |
| `scenario_path` | Source scenario YAML — used to seed `name`, `pre`, `post`, `allocate_ports`. |
| `session_id` | Which session this recording is for (must match the `session_id` passed to `start_session`). |
| `driver` | Free-text label recorded in the plan header (default `copilot-cli`). |

The recorder auto-seeds each step with:

- `contains:` — last 3 non-empty, non-noise lines of the post-step
  capture (delta-aware vs. the previous capture).
- `not_contains:` — `["Traceback", "error:"]` as a safety net.
- `timeout_seconds:` — 10s (bumped to 30s if `observe()` polled longer
  than 5s during recording).

**Trim the `contains` lines down to the meaningful assertions before
committing.** The recorder's output is a strong first draft, not a
finished test.

### Plan schema (v1)

```yaml
schema_version: 1
name: smoke-test
source_scenario: scenarios/smoke-test.yaml
command: "echo 'Hello' && echo 'Done'"
cwd: "/tmp"
env: {}
allocate_ports: []
pre: []
post: []

steps:
  - index: 0
    kind: start                 # captured after AgentSession.start()
    assert:
      contains: ["Hello"]
      not_contains: ["Traceback"]
      timeout_seconds: 10

  - index: 1
    kind: action                # one driver-initiated act() call
    action: { action: "select", choice_text: "Python" }
    assert:
      contains: ["Selected Python"]
      timeout_seconds: 10

  - index: 2
    kind: observe               # explicit post-action check
    assert:
      contains: ["Done"]
      session_exited: true

  - index: 3
    kind: screenshot            # SVG written to reports/, no assertions
    label: "final state"
```

Step kinds: `start`, `action`, `observe`, `screenshot`.
Assertion vocabulary: `contains`, `not_contains`, `regex`,
`session_exited`, `timeout_seconds`. Replay is **fail-fast** — the
first failing step ends the run with a non-zero exit code, but
`finish_session` and `post` hooks always execute so CI artifacts are
intact.

### Replaying locally

```bash
.venv/bin/cli-tester-replay plans/smoke-test.plan.yaml
echo $?    # 0 = pass, 1 = assertion failed, 2 = usage / plan-load error
```

The HTML report + SVG screenshots land under
`reports/replay_<plan>_<timestamp>/` just like interactive runs.

### CI workflow contract

`.github/workflows/ci.yml` runs on every push and PR with two jobs on
`ubuntu-latest`:

1. **Unit tests** — `pytest -q`. Hermetic; no tmux interaction.
2. **Replay** — discovers `plans/*.plan.yaml`, fans out one matrix job
   per plan, installs `tmux`, runs `cli-tester-replay <plan>`. On
   failure, uploads `reports/**` as an artifact for inspection.

No secrets are required for v1. Scenarios that need cloud credentials
(e.g. `azd` commands that hit Azure) intentionally do **not** have
plans committed and stay out of the matrix until we add an auth design.

### When a plan fails in CI

A failure means one of two things:

- The target CLI's UI legitimately changed → re-record the plan, review
  the diff alongside the code change.
- The replay surfaced a real regression in the CLI under test → that's
  the whole point. Read the failing capture from the uploaded
  `reports/**` artifact.

Don't paper over failures by relaxing assertions just to make CI green
— that defeats the regression-detection value.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "tmux is required" | macOS: `brew install tmux`. Linux: `sudo apt install tmux`. Windows: install tmux in WSL **and** register the MCP server to launch via `wsl` (see Windows setup) — running the server from Windows-side Python will not see tmux. |
| MCP server not found | Check the `command` path in mcp-config.json points to the venv Python |
| "No active session" | Call `start_session` before `observe`/`send_action` |
| Terminal shows old state | Call `observe` — it waits for content to change |
| Stuck on a prompt | The `send_action` wait type can pause; or try a `select` with `choice_index: 0` to accept the highlighted default |
| `record_plan` not found by Copilot CLI | Restart your Copilot CLI session — FastMCP captures tool schemas at server startup; new tools aren't visible to an in-flight session. |
