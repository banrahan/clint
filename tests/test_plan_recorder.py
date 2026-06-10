"""Hermetic tests for PlanRecorder.

Drive a real AgentSession with patched tmux primitives, assert that the
recorder emits a sane plan YAML with the expected shape and assertions.
"""
from __future__ import annotations

import os

import pytest
import yaml

from auto_test_tool import agent as agent_mod
from auto_test_tool import runner as runner_mod
from auto_test_tool.agent import AgentSession
from auto_test_tool.recorder import (
    DEFAULT_NOT_CONTAINS,
    SCHEMA_VERSION,
    PlanRecorder,
    _seed_contains,
)


class FakeTmux:
    """Same shape as test_select_by_text.FakeTmux but local to keep tests
    self-contained."""

    def __init__(self, captures):
        self._captures = list(captures)
        self.events: list[tuple[str, str]] = []

    def capture(self, session, with_ansi=False):
        if len(self._captures) > 1:
            return self._captures.pop(0)
        return self._captures[0]

    def send_keys(self, session, key):
        self.events.append(("key", key))

    def send_text(self, session, text):
        self.events.append(("text", text))


@pytest.fixture
def patch_tmux_for_session(monkeypatch):
    """Patch everything AgentSession touches so it never spawns tmux."""

    def install(captures):
        fake = FakeTmux(captures)
        # agent.py imports these at module load and uses them directly.
        monkeypatch.setattr(agent_mod, "tmux_capture_pane", fake.capture)
        monkeypatch.setattr(agent_mod, "tmux_send_keys", fake.send_keys)
        monkeypatch.setattr(agent_mod, "tmux_send_text", fake.send_text)
        # AgentSession.start() / finish() also call create/kill/alive — make
        # these no-ops so the session lifecycle works without tmux.
        monkeypatch.setattr(agent_mod, "tmux_create_session", lambda *a, **kw: None)
        monkeypatch.setattr(agent_mod, "tmux_kill_session", lambda *a, **kw: None)
        monkeypatch.setattr(agent_mod, "tmux_session_alive", lambda *a, **kw: True)
        # Speed up.
        monkeypatch.setattr(agent_mod.time, "sleep", lambda *a, **kw: None)
        # The screenshot / report generation paths call render_ansi_to_svg /
        # save_text_capture / generate_html_report from runner.py — let
        # them run for fidelity, they only touch local files in tmp_path.
        return fake

    return install


def test_seed_contains_prefers_new_lines():
    prev = "old line 1\nold line 2"
    new = "old line 1\nbrand new line\nanother brand new line"
    result = _seed_contains(prev, new)
    # Only the two new lines, in order.
    assert result == ["brand new line", "another brand new line"]


def test_seed_contains_falls_back_to_tail_when_no_delta():
    prev = "same line"
    new = "same line"
    # No delta at all → fall back to tail of the new capture, not empty.
    result = _seed_contains(prev, new)
    assert result == ["same line"]


def test_seed_contains_skips_shell_prompt_noise():
    new = "$ echo hello\nhello\nDone"
    # `$ echo hello` is shell-prompt noise; should be skipped.
    result = _seed_contains("", new)
    assert "$ echo hello" not in result
    assert "Done" in result


def test_recorder_emits_v1_plan_with_start_action_screenshot(
    patch_tmux_for_session, tmp_path
):
    captures = [
        "Welcome to fake-cli\n? Pick a flavor:\n> Vanilla\n  Chocolate",
        "Welcome to fake-cli\n? Pick a flavor:\n  Vanilla\n> Chocolate",
        "You picked Chocolate\nDone",
    ]
    patch_tmux_for_session(captures)

    plan_path = tmp_path / "plans" / "test.plan.yaml"
    session = AgentSession(
        command="fake-cli",
        cwd=str(tmp_path),
        output_dir=str(tmp_path / "reports"),
        run_name="test_run",
    )
    recorder = PlanRecorder(
        source_scenario="scenarios/fake.yaml",
        scenario_data={
            "name": "fake-scenario",
            "allocate_ports": ["agent"],
            "pre": ["mkdir -p /tmp/foo"],
            "post": [],
        },
        plan_path=str(plan_path),
        driver="pytest",
    )
    recorder.attach(session)

    session.start()
    session.act({"action": "select", "choice_index": 1})
    session.screenshot(label="after-pick")
    session.finish()

    assert plan_path.exists(), "recorder should have written plan on finish()"
    text = plan_path.read_text()
    assert text.startswith("# AUTO-GENERATED"), "header comment missing"
    assert "Driver: pytest" in text

    plan = yaml.safe_load(text)
    assert plan["schema_version"] == SCHEMA_VERSION
    assert plan["name"] == "fake-scenario"
    assert plan["source_scenario"] == "scenarios/fake.yaml"
    assert plan["command"] == "fake-cli"
    assert plan["allocate_ports"] == ["agent"]
    assert plan["pre"] == ["mkdir -p /tmp/foo"]
    assert plan["post"] == []

    kinds = [s["kind"] for s in plan["steps"]]
    assert kinds == ["start", "action", "screenshot"], kinds

    start_step = plan["steps"][0]
    assert start_step["assert"]["not_contains"] == DEFAULT_NOT_CONTAINS
    assert start_step["assert"]["contains"], "start step should seed contains"

    action_step = plan["steps"][1]
    assert action_step["action"] == {"action": "select", "choice_index": 1}
    assert "assert" in action_step

    shot_step = plan["steps"][2]
    assert shot_step["label"] == "after-pick"
    assert "assert" not in shot_step, "screenshot step should have no assertions"


def test_recorder_default_plan_path_uses_scenario_stem(
    patch_tmux_for_session, tmp_path, monkeypatch
):
    patch_tmux_for_session(["x"])
    monkeypatch.chdir(tmp_path)

    session = AgentSession(
        command="echo hi", cwd=str(tmp_path),
        output_dir=str(tmp_path / "reports"), run_name="t",
    )
    recorder = PlanRecorder(source_scenario="scenarios/awesome.yaml")
    recorder.attach(session)

    session.start()
    session.finish()

    expected = tmp_path / "plans" / "awesome.plan.yaml"
    assert expected.exists()


def test_recorder_marks_session_exited_when_capture_has_marker(
    patch_tmux_for_session, tmp_path, monkeypatch
):
    # First the post-start capture, then for the action: simulate the
    # tmux session having exited so AgentSession.observe() prefixes
    # [SESSION EXITED]\n to the capture.
    patch_tmux_for_session(["hello", "Done\nshell prompt back"])
    # After start() runs, mark the session dead for subsequent observe() calls.
    state = {"alive": True}
    monkeypatch.setattr(
        agent_mod, "tmux_session_alive", lambda *a, **kw: state["alive"]
    )

    session = AgentSession(
        command="echo done", cwd=str(tmp_path),
        output_dir=str(tmp_path / "reports"), run_name="t",
    )
    recorder = PlanRecorder(
        source_scenario="scenarios/exit.yaml",
        plan_path=str(tmp_path / "exit.plan.yaml"),
    )
    recorder.attach(session)

    session.start()
    state["alive"] = False  # next observe() will see exit and add marker
    session.act({"action": "wait", "seconds": 0})
    session.finish()

    plan = yaml.safe_load((tmp_path / "exit.plan.yaml").read_text())
    action_step = plan["steps"][1]
    assert action_step["assert"]["session_exited"] is True
    # The marker text should not leak into seeded contains.
    for needle in action_step["assert"].get("contains", []):
        assert "SESSION EXITED" not in needle
