"""End-to-end test of scenario YAML → port pool → template substitution
via the MCP server boundary (without launching real tmux)."""

from pathlib import Path

import pytest

from clint import mcp_server, ports


@pytest.fixture(autouse=True)
def _clean_registry():
    ports.reset_registry()
    yield
    ports.reset_registry()


def _write_scenario(tmp_path: Path, body: str) -> str:
    p = tmp_path / "scenario.yaml"
    p.write_text(body)
    return str(p)


def test_load_scenario_substitutes_port_in_command_and_goals(tmp_path):
    scenario = _write_scenario(
        tmp_path,
        """
name: parallel-agent
allocate_ports: [agent]
command: "azd ai agent run --port {agent}"
goals:
  - "Confirm agent listens on {agent}"
  - "Invoke: azd ai agent invoke --local --port {agent} 'Hi'"
""",
    )
    out = mcp_server._read_scenario_file(scenario)
    pool = ports.get_pool(scenario, ["agent"])
    port = pool.get("agent")
    assert f"--port {port}" in out
    assert f"Allocated ports: agent={port}" in out
    assert f"Confirm agent listens on {port}" in out
    assert f"invoke --local --port {port}" in out
    assert "{agent}" not in out


def test_load_scenario_numbered_port_alias(tmp_path):
    scenario = _write_scenario(
        tmp_path,
        """
name: numbered
allocate_ports: 1
command: "run --port {port}"
goals:
  - "Use {port1}"
""",
    )
    out = mcp_server._read_scenario_file(scenario)
    pool = ports.get_pool(scenario, 1)
    p = pool.get("port1")
    assert f"--port {p}" in out
    assert f"Use {p}" in out


def test_load_scenario_no_ports_passes_through_unchanged(tmp_path):
    scenario = _write_scenario(
        tmp_path,
        """
name: noports
command: "echo hi"
goals:
  - "Plain goal with {{literal}} braces"
""",
    )
    out = mcp_server._read_scenario_file(scenario)
    assert "Allocated ports" not in out
    assert "{literal}" in out


def test_load_scenario_unknown_placeholder_does_not_crash(tmp_path):
    """{session_var} only resolves at start_session time. load_scenario
    should display the literal rather than refusing."""
    scenario = _write_scenario(
        tmp_path,
        """
name: needs-session-var
command: "echo {session_var}"
goals:
  - "Run with {session_var}"
""",
    )
    out = mcp_server._read_scenario_file(scenario)
    assert "{session_var}" in out


def test_run_phase_substitutes_in_hook_run(tmp_path, monkeypatch):
    scenario = _write_scenario(
        tmp_path,
        """
name: hook-port
allocate_ports: [agent]
command: "echo hi"
pre:
  - "echo agent on {agent}"
""",
    )

    captured = {}

    from clint import hooks as hooks_mod

    def fake_execute(hook_list):
        captured["hooks"] = list(hook_list)
        return [hooks_mod.HookResult(hook=h, exit_code=0) for h in hook_list]

    monkeypatch.setattr(mcp_server, "execute_hooks", fake_execute)

    out = mcp_server._run_phase(scenario, "pre")
    assert captured["hooks"], "execute_hooks was not called"
    pool = ports.get_pool(scenario, ["agent"])
    p = pool.get("agent")
    assert captured["hooks"][0].run == f"echo agent on {p}"
    assert "OK" in out


def test_run_phase_invalid_placeholder_reports_error(tmp_path):
    scenario = _write_scenario(
        tmp_path,
        """
name: bad-placeholder
command: "echo hi"
pre:
  - "echo {nope}"
""",
    )
    out = mcp_server._run_phase(scenario, "pre")
    assert out.startswith("ERROR:")
    assert "nope" in out


def test_resolve_vars_uses_same_pool_as_get_pool(tmp_path):
    scenario = _write_scenario(
        tmp_path,
        """
name: shared
allocate_ports: [agent]
command: "echo {agent}"
""",
    )
    mcp_server._read_scenario_file(scenario)
    vars_dict, pool = mcp_server._resolve_vars(scenario, None)
    assert pool is not None
    pool2 = ports.get_pool(scenario, ["agent"])
    assert pool is pool2
    assert vars_dict["agent"] == pool.get("agent")


def test_load_scenario_substitutes_cwd_in_command(tmp_path):
    """{cwd} in command/goals resolves to the scenario's resolved cwd
    (including any {instance} expansion)."""
    scenario = _write_scenario(
        tmp_path,
        """
name: needs-cwd
command: "copilot --add-dir {cwd}"
cwd: "/tmp/work-{instance}"
goals:
  - "Run inside {cwd}"
""",
    )
    out = mcp_server._read_scenario_file(scenario)
    assert "--add-dir /tmp/work-main" in out
    assert "Run inside /tmp/work-main" in out
    assert "{cwd}" not in out


def test_run_phase_substitutes_cwd_in_hook(tmp_path, monkeypatch):
    scenario = _write_scenario(
        tmp_path,
        """
name: hook-cwd
command: "echo hi"
cwd: "/tmp/work-{instance}"
pre:
  - "mkdir -p {cwd}"
""",
    )

    captured = {}

    from clint import hooks as hooks_mod

    def fake_execute(hook_list):
        captured["hooks"] = list(hook_list)
        return [hooks_mod.HookResult(hook=h, exit_code=0) for h in hook_list]

    monkeypatch.setattr(mcp_server, "execute_hooks", fake_execute)

    mcp_server._run_phase(scenario, "pre")
    assert captured["hooks"][0].run == "mkdir -p /tmp/work-main"


def test_start_session_substitutes_cwd_in_command(tmp_path, monkeypatch):
    """start_session should expose the resolved cwd as {cwd} for the command."""
    captured = {}

    class FakeSession:
        def __init__(self, *, command, cwd, env, output_dir, run_name, session_id):
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env
            self.cwd = cwd
            self.run_dir = "/tmp/fake"

        def start(self):
            return "(fake terminal)"

    monkeypatch.setattr(mcp_server, "AgentSession", FakeSession)
    monkeypatch.setattr(mcp_server, "tmux_is_installed", lambda: True)
    # Isolate session registry so we don't collide with other tests.
    monkeypatch.setattr(mcp_server, "_sessions", {})
    monkeypatch.setattr(mcp_server, "_session_scenarios", {})

    scenario = _write_scenario(
        tmp_path,
        """
name: cwd-in-command
command: "copilot --add-dir {cwd}"
cwd: "/tmp/work-{instance}"
""",
    )

    out = mcp_server.start_session(
        command="copilot --add-dir {cwd}",
        cwd="/tmp/work-{instance}",
        scenario_path=scenario,
        session_id="t-cwd",
    )
    assert "ERROR" not in out
    assert captured["command"] == "copilot --add-dir /tmp/work-main"
    assert captured["cwd"] == "/tmp/work-main"


def test_start_session_explicit_cwd_var_overrides(tmp_path, monkeypatch):
    """If the caller passes session_vars['cwd'], it wins over the resolved cwd."""
    captured = {}

    class FakeSession:
        def __init__(self, *, command, cwd, env, output_dir, run_name, session_id):
            captured["command"] = command
            captured["cwd"] = cwd
            self.cwd = cwd
            self.run_dir = "/tmp/fake"

        def start(self):
            return "(fake terminal)"

    monkeypatch.setattr(mcp_server, "AgentSession", FakeSession)
    monkeypatch.setattr(mcp_server, "tmux_is_installed", lambda: True)
    monkeypatch.setattr(mcp_server, "_sessions", {})
    monkeypatch.setattr(mcp_server, "_session_scenarios", {})

    scenario = _write_scenario(
        tmp_path,
        """
name: cwd-override
command: "echo {cwd}"
cwd: "/tmp/resolved"
""",
    )

    mcp_server.start_session(
        command="echo {cwd}",
        cwd="/tmp/resolved",
        scenario_path=scenario,
        session_id="t-cwd-override",
        session_vars={"cwd": "/explicit/override"},
    )
    assert captured["command"] == "echo /explicit/override"
