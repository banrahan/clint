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


def test_recorder_seeds_from_settled_capture_not_post_action_transient(
    patch_tmux_for_session, tmp_path
):
    """The seed capture for step N is taken right before step N+1 runs,
    so transient text that AgentSession.observe() may have grabbed
    immediately post-action (e.g. a "Loading..." spinner) is gone by
    seeding time and does NOT appear in `contains`.

    Capture pop ordering (FakeTmux pops one per call until 1 left):
      1. start() -> session.observe()         (welcome / first prompt)
      2. recorder pre-act#1 _capture_text_now (settled first prompt)
      3. act#1 internal screenshot (ANSI)     (ignored for seeding)
      4. act#1 post-keys observe()            (TRANSIENT: "Loading...")
      5. recorder pre-act#2 _capture_text_now (SETTLED: spinner gone)
      6. act#2 internal screenshot
      7. act#2 post-keys observe()            (final state)
      8. finish() settle loop reads (returns the last padded value)
    """
    captures = [
        "Welcome\n? Pick a flavor:\n> Vanilla\n  Chocolate",   # 1: start observe
        "Welcome\n? Pick a flavor:\n> Vanilla\n  Chocolate",   # 2: settled
        "Welcome\n? Pick a flavor:\n> Vanilla\n  Chocolate",   # 3: act1 ANSI
        # Post-act#1: a transient spinner is on screen
        "? Pick a model:\nLoading models...\n  spinner",       # 4: transient
        # By the time the driver calls act#2, the spinner is gone:
        "? Pick a model:\n> gpt-4\n  gpt-3",                   # 5: SETTLED
        "? Pick a model:\n> gpt-4\n  gpt-3",                   # 6: act2 ANSI
        "All done!",                                            # 7: act2 observe
        "All done!",                                            # 8+: settle loop
    ]
    patch_tmux_for_session(captures)

    plan_path = tmp_path / "transient.plan.yaml"
    session = AgentSession(
        command="fake-cli",
        cwd=str(tmp_path),
        output_dir=str(tmp_path / "reports"),
        run_name="t",
    )
    recorder = PlanRecorder(
        source_scenario="scenarios/transient.yaml",
        plan_path=str(plan_path),
    )
    recorder.attach(session)

    session.start()
    session.act({"action": "select", "choice_index": 0})
    session.act({"action": "select", "choice_index": 0})
    session.finish()

    plan = yaml.safe_load(plan_path.read_text())
    steps = plan["steps"]
    assert [s["kind"] for s in steps] == ["start", "action", "action"]

    # Step 1 (first action) — its `contains` should describe the SETTLED
    # state the driver saw before issuing act#2, not the spinner that
    # was visible during the brief post-keystroke window.
    act1_contains = steps[1]["assert"]["contains"]
    joined = "\n".join(act1_contains)
    assert "Loading models..." not in joined, (
        "transient 'Loading models...' must NOT appear in seeded contains; "
        f"got: {act1_contains}"
    )
    assert any("gpt-4" in line for line in act1_contains), (
        f"settled menu line should appear in contains; got: {act1_contains}"
    )


def test_recorder_seeds_trailing_step_via_settle_loop(
    patch_tmux_for_session, tmp_path, monkeypatch
):
    """When finish() runs and there's still a pending step (the most
    common case — the last action has no follow-up), the recorder uses
    a settle loop to obtain a stable capture for seeding."""
    captures = [
        "intro",         # 1: start observe
        "intro",         # 2+: settle loop reads (stable)
    ]
    patch_tmux_for_session(captures)
    # Make sleep a no-op so the settle loop runs at wall-clock zero.
    # The fixture already patches agent_mod.time.sleep; patch the
    # recorder module's time.sleep too.
    from auto_test_tool import recorder as recorder_mod
    monkeypatch.setattr(recorder_mod.time, "sleep", lambda *a, **kw: None)

    plan_path = tmp_path / "trailing.plan.yaml"
    session = AgentSession(
        command="echo hi", cwd=str(tmp_path),
        output_dir=str(tmp_path / "reports"), run_name="t",
    )
    recorder = PlanRecorder(
        source_scenario="scenarios/trailing.yaml",
        plan_path=str(plan_path),
    )
    recorder.attach(session)

    session.start()
    session.finish()

    plan = yaml.safe_load(plan_path.read_text())
    assert [s["kind"] for s in plan["steps"]] == ["start"]
    start = plan["steps"][0]
    # contains must have been seeded by the settle-loop branch in finish.
    assert "intro" in start["assert"]["contains"]

