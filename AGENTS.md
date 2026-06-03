# Agent guidelines for `cli-interactive-tester`

This file is read automatically by AI coding assistants (Copilot CLI,
Codex, Claude Code, etc.). It captures non-obvious conventions for
**both** working on this repo **and** driving the MCP server it ships.

These rules have been earned the hard way — each one exists because
something went wrong before it. Don't relax them without a good reason.

---

## Driving the MCP (scenario execution)

When you are the agent driving `cli-interactive-tester`'s MCP tools to
run a scenario:

### Do NOT verify after `select(choice_text=...)` — these runs are tests

This tool exists to **test** interactive CLIs end-to-end. If the
driving agent verifies each pick by reading back the echo line and
correcting course on mismatch, it papers over the exact bugs the test
is supposed to catch (silent mispicks in `select_by_text`, picker
regressions in the CLI under test, etc.). The whole point of running
the scenario is to find out whether the tool gets it right unaided.

Rules:

* **Send the action and move on.** Don't add an `observe`-then-
  `assert` step after every `select`. Trust the action; let downstream
  prompts surface any failure naturally (e.g. `azd provision` blowing
  up because the wrong subscription was picked is itself the signal).
* **Don't quietly retry on mismatch.** If you suspect a mispick — say,
  via a fail-loud `LookupError` surfaced as `ERROR during 'select': …`
  — stop and ask the user. Don't paper over it with `choice_index` or
  a more specific `choice_text` on your own initiative; that hides the
  defect from the test run.
* **Don't pre-confirm before destructive actions either.** If the
  scenario says "select benhanrahan then provision", drive that flow;
  the test result is the truth. (The one exception is the "ask before
  the first cloud-creating action of the session" rule below — that's
  about user consent to spending, not about verifying picks.)

The fail-loud surface (`LookupError` → `ERROR during 'select': …`) is
still useful: it tells you the tool *couldn't* find the target at all
(as opposed to silently picking something wrong). Treat it as a
genuine failure and report it — don't retry around it.

### Prefer `choice_text` over `choice_index` when the label is stable

Indices shift when picker contents change between releases. Use
`choice_index` only for prompts whose ordering you control. This is a
test-authoring preference, not a verification step — pick once and
move on.

### Parallel instances need `instance_id`

To run N concurrent instances of the *same* scenario:

```text
start_session(scenario_path=..., instance_id="1", session_id="run-1", ...)
start_session(scenario_path=..., instance_id="2", session_id="run-2", ...)
```

Each `instance_id` gets its own port pool (no `{port}` collisions) and
exposes `{instance}` for substitution into `cwd`, resource names, etc.
Omitting `instance_id` defaults `{instance}` to `"main"`. See README
"Running N parallel instances of one scenario" for the full pattern.

### Pause before destructive or cloud-creating actions

Driving a scenario through `azd init` / `azd provision` / `terraform
apply` / any "create real resources" step is **irreversible-ish** and
expensive. If any of the following are true, stop and ask the user
before continuing past the gate:

* The scenario is being run in parallel — N wrong-sub provisions hurt
  N times more than one.
* You're about to enter the provisioning phase for the first time in
  the session (the user may want to dry-run with `--preview` or just
  exercise the picker flow without spending money).

Use `ask_user` with a small set of choices (e.g. "Continue full
flow / Stop here / Abort and retry with fix"). Don't proceed on
implied consent.

The earlier "just go full flow" answer to a generic question is **not**
license to push through later anomalies. Re-confirm at each anomaly.

### When the MCP behaves unexpectedly, read its source

If a tool returns something surprising (silently picks the wrong item,
returns the wrong type, ignores a parameter), open the implementation
in `auto_test_tool/` and trace the call path **before** retrying or
working around it. The "Project Vienna" mispick that birthed this
file's verification rule was a 12-line silent-fallback bug that two
retries would have repeated. Reading `agent.py` for ~30 seconds was
the fastest path to a real fix.

---

## Working on this repo

### Design principle: fail loud, never silently fall back

This is a *test runner*. Silent fallbacks that "succeed" with the wrong
answer are the worst possible failure mode — they look green on the
dashboard while doing nothing useful. Whenever you add a code path
that might not match what the caller asked for:

* **Raise.** Use a clear exception type (`LookupError`, `ValueError`)
  with the searched-for value and the visible context in the message.
* **Don't `return None`** as a "best effort" signal — callers won't
  check it consistently.
* **Surface the error end-to-end.** `mcp_server.send_action` prepends
  `ERROR during 'select': …` to the response body so the driving
  agent can't miss it. Mirror that pattern for any new failure mode.

Counter-example to avoid (the bug fixed in this repo's history):
`select_by_text` used to press Enter blindly after 25 unsuccessful
arrow scrolls, picking whatever happened to be highlighted. Don't
write code shaped like that.

### Centralize defaults, don't sprinkle them

When you add a default value (e.g. `{instance}` → `"main"`), put it in
one place that all entry points route through (see
`mcp_server._resolve_vars`). Avoid having `start_session`,
`_read_scenario_file`, and `_run_phase` each independently default the
same variable — they'll drift.

### Tests are hermetic — keep them that way

All tests under `tests/` run without touching tmux, Azure, or any
external service. The tmux primitives in `auto_test_tool.agent`
(`tmux_capture_pane`, `tmux_send_keys`, `tmux_send_text`) are imported
at module level so they can be monkeypatched. Follow the
`FakeTmux` pattern in `tests/test_select_by_text.py` for any new
picker/keystroke logic.

Run:

```bash
.venv/bin/python -m pytest -q
```

### Env / install

The repo uses `uv` and a `.venv`. There is **no `pip` inside the venv**
— install with:

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
```

### MCP server reload requires Copilot CLI restart

FastMCP captures tool schemas (parameter lists, descriptions) **at
server startup**. If you change `mcp_server.py` — add a new tool,
add a parameter, rename a field — the Copilot CLI session that's
currently connected will keep using the old schema. You cannot reload
it from inside the session.

When you ship MCP-shape changes and want to drive them live, tell the
user they need to restart Copilot CLI (or whichever client is
hosting the MCP) before you can call the new surface. Don't try to
work around it — you'll get cryptic `NoneType` errors from missing
fields.

### Where new "agent driving" behavioural rules go

If you change how the MCP should be driven (new picker semantics, new
required verification step, new error surface), update **both**:

1. The "Driving the MCP" section of this file, and
2. The relevant README section (so humans browsing the repo see it).

Don't rely on commit messages or chat history — the next agent session
won't read them.

### Adding new rules to this file

When in doubt, ask: *"would the next agent session repeat the mistake
that taught me this rule?"* If yes, it belongs here. Keep each rule
tied to a concrete failure or design constraint so future readers
understand why it exists and when it's safe to relax.
