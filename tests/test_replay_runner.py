"""Hermetic tests for the replay runner.

These bypass tmux entirely by patching the same primitives the recorder
tests patch, then feed scripted plans into ``replay()`` and assert on
outcomes / exit codes.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from auto_test_tool import agent as agent_mod
from auto_test_tool import replay as replay_mod
from auto_test_tool.replay import (
    AssertionFailure,
    Plan,
    PlanStep,
    _check_not_contains,
    _check_regex,
    _wait_for_contains,
    load_plan,
    main,
    replay,
)


class FakeTmux:
    def __init__(self, captures):
        # captures is a list — pop from front, last one is sticky.
        self._captures = list(captures) or [""]
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
def patch_tmux(monkeypatch):
    """Patch tmux primitives for both agent.py and replay.py."""

    def install(captures, alive: bool = True):
        fake = FakeTmux(captures)
        monkeypatch.setattr(agent_mod, "tmux_capture_pane", fake.capture)
        monkeypatch.setattr(agent_mod, "tmux_send_keys", fake.send_keys)
        monkeypatch.setattr(agent_mod, "tmux_send_text", fake.send_text)
        monkeypatch.setattr(agent_mod, "tmux_create_session", lambda *a, **kw: None)
        monkeypatch.setattr(agent_mod, "tmux_kill_session", lambda *a, **kw: None)

        state = {"alive": alive}
        monkeypatch.setattr(
            agent_mod, "tmux_session_alive", lambda *a, **kw: state["alive"]
        )
        # replay.py also looks up session_alive for session_exited assertions.
        monkeypatch.setattr(
            replay_mod, "tmux_session_alive", lambda *a, **kw: state["alive"]
        )
        # The CLI entry point checks tmux installability — pretend yes.
        monkeypatch.setattr(replay_mod, "tmux_is_installed", lambda: True)

        monkeypatch.setattr(agent_mod.time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(replay_mod.time, "sleep", lambda *a, **kw: None)
        return fake, state

    return install


# ----------------------------------------------------------------------
# Assertion-primitive unit tests
# ----------------------------------------------------------------------


def test_wait_for_contains_returns_immediately_when_satisfied():
    class DummySession:
        def observe(self):  # pragma: no cover - should not be called
            raise AssertionError("observe should not be called")

    out = _wait_for_contains(DummySession(), ["foo"], 5.0, "foo bar baz")
    assert out == "foo bar baz"


def test_wait_for_contains_times_out_when_missing(monkeypatch):
    class DummySession:
        def observe(self):
            return "nothing here"

    monkeypatch.setattr(replay_mod.time, "sleep", lambda *a, **kw: None)
    # Force the loop to exit quickly by zeroing the deadline.
    with pytest.raises(AssertionFailure) as exc:
        _wait_for_contains(DummySession(), ["WANTED"], 0.0, "nothing here")
    assert "WANTED" in str(exc.value)


def test_check_not_contains_fails_when_forbidden_text_present():
    with pytest.raises(AssertionFailure) as exc:
        _check_not_contains("hello Traceback (most recent)", ["Traceback"])
    assert "Traceback" in str(exc.value)


def test_check_not_contains_passes_when_clean():
    # Should not raise.
    _check_not_contains("all good", ["Traceback", "error:"])


def test_check_regex_passes_when_pattern_matches():
    _check_regex("Listening on port 8088", [r"port \d+"])


def test_check_regex_fails_when_pattern_missing():
    with pytest.raises(AssertionFailure):
        _check_regex("no numbers here", [r"port \d+"])


# ----------------------------------------------------------------------
# Plan loading
# ----------------------------------------------------------------------


def test_load_plan_parses_v1_schema(tmp_path):
    plan_dict = {
        "schema_version": 1,
        "name": "ex",
        "command": "echo hi",
        "cwd": "/tmp",
        "env": {},
        "allocate_ports": [],
        "pre": [],
        "post": [],
        "steps": [
            {"index": 0, "kind": "start", "assert": {"contains": ["hi"]}},
            {
                "index": 1,
                "kind": "action",
                "action": {"action": "wait", "seconds": 0},
                "assert": {"contains": ["done"], "timeout_seconds": 5},
            },
        ],
    }
    p = tmp_path / "ex.plan.yaml"
    p.write_text(yaml.safe_dump(plan_dict))
    plan = load_plan(str(p))
    assert plan.name == "ex"
    assert len(plan.steps) == 2
    assert plan.steps[1].timeout_seconds == 5.0


def test_load_plan_rejects_unknown_schema_version(tmp_path):
    p = tmp_path / "bad.plan.yaml"
    p.write_text(yaml.safe_dump({"schema_version": 99, "command": "x", "steps": []}))
    with pytest.raises(ValueError, match="schema_version"):
        load_plan(str(p))


def test_load_plan_rejects_unknown_step_kind(tmp_path):
    p = tmp_path / "bad.plan.yaml"
    p.write_text(yaml.safe_dump({
        "schema_version": 1,
        "command": "x",
        "steps": [{"index": 0, "kind": "frobnicate"}],
    }))
    with pytest.raises(ValueError, match="kind"):
        load_plan(str(p))


# ----------------------------------------------------------------------
# End-to-end replay against FakeTmux
# ----------------------------------------------------------------------


def _make_plan(steps, tmp_path, command="echo hi"):
    return Plan(
        schema_version=1,
        name="t",
        source_scenario=None,
        command=command,
        cwd=str(tmp_path),
        env={},
        allocate_ports=None,
        pre=[],
        post=[],
        steps=steps,
    )


def test_replay_passes_when_assertions_satisfied(patch_tmux, tmp_path):
    patch_tmux(["Hello world\nReady"])
    plan = _make_plan(
        [
            PlanStep(
                index=0, kind="start",
                contains=["Hello world", "Ready"],
                not_contains=["Traceback"],
                timeout_seconds=1.0,
            ),
        ],
        tmp_path,
    )
    result = replay(plan, output_dir=str(tmp_path / "reports"), run_name="r")
    assert result.success
    assert len(result.outcomes) == 1
    assert result.outcomes[0].success


def test_replay_fails_when_contains_missing(patch_tmux, tmp_path):
    patch_tmux(["Hello world"])
    plan = _make_plan(
        [
            PlanStep(
                index=0, kind="start",
                contains=["NOT THERE"],
                timeout_seconds=0.0,
            ),
        ],
        tmp_path,
    )
    result = replay(plan, output_dir=str(tmp_path / "reports"), run_name="r")
    assert not result.success
    assert "NOT THERE" in result.outcomes[0].error


def test_replay_fails_fast_and_skips_remaining_steps(patch_tmux, tmp_path):
    patch_tmux(["only this"])
    plan = _make_plan(
        [
            PlanStep(
                index=0, kind="start",
                contains=["only this"], timeout_seconds=1.0,
            ),
            PlanStep(
                index=1, kind="observe",
                contains=["MISSING"], timeout_seconds=0.0,
            ),
            PlanStep(
                index=2, kind="screenshot", label="should-not-run",
            ),
        ],
        tmp_path,
    )
    result = replay(plan, output_dir=str(tmp_path / "reports"), run_name="r")
    assert not result.success
    # Fail-fast: only the first two outcomes are recorded; index 2 is skipped.
    assert [o.index for o in result.outcomes] == [0, 1]
    assert result.outcomes[0].success
    assert not result.outcomes[1].success


def test_replay_session_exited_assertion(patch_tmux, tmp_path):
    _, state = patch_tmux(["banner\nReady"])
    # Mark dead so session_exited:true should pass.
    state["alive"] = False
    plan = _make_plan(
        [
            PlanStep(
                index=0, kind="observe",
                session_exited=True,
                timeout_seconds=0.0,
            ),
        ],
        tmp_path,
    )
    result = replay(plan, output_dir=str(tmp_path / "reports"), run_name="r")
    assert result.success, result.outcomes[0].error


def test_replay_not_contains_catches_tracebacks(patch_tmux, tmp_path):
    patch_tmux(["Loading...\nTraceback (most recent call last):\n  File ..."])
    plan = _make_plan(
        [
            PlanStep(
                index=0, kind="start",
                not_contains=["Traceback"], timeout_seconds=1.0,
            ),
        ],
        tmp_path,
    )
    result = replay(plan, output_dir=str(tmp_path / "reports"), run_name="r")
    assert not result.success
    assert "Traceback" in result.outcomes[0].error


def test_replay_records_action_keystrokes_via_agent(patch_tmux, tmp_path):
    fake, _ = patch_tmux(["? Pick:\n> Yes", "You picked Yes"])
    plan = _make_plan(
        [
            PlanStep(index=0, kind="start", timeout_seconds=1.0),
            PlanStep(
                index=1, kind="action",
                action={"action": "confirm", "value": True},
                contains=["You picked Yes"], timeout_seconds=1.0,
            ),
        ],
        tmp_path,
    )
    result = replay(plan, output_dir=str(tmp_path / "reports"), run_name="r")
    assert result.success, [o.error for o in result.outcomes]
    # confirm sends 'y' then 'Enter'.
    keys = [e for e in fake.events if e[0] == "key"]
    assert ("key", "Enter") in keys


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------


def test_cli_returns_zero_on_success(patch_tmux, tmp_path, capsys):
    patch_tmux(["Hello"])
    plan_path = tmp_path / "ok.plan.yaml"
    plan_path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "name": "ok",
        "command": "echo hi",
        "cwd": str(tmp_path),
        "env": {},
        "allocate_ports": [],
        "pre": [],
        "post": [],
        "steps": [
            {"index": 0, "kind": "start",
             "assert": {"contains": ["Hello"], "timeout_seconds": 1}},
        ],
    }))
    rc = main([str(plan_path), "--output-dir", str(tmp_path / "reports")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out


def test_cli_returns_one_on_assertion_failure(patch_tmux, tmp_path, capsys):
    patch_tmux(["Hello"])
    plan_path = tmp_path / "fail.plan.yaml"
    plan_path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "name": "fail",
        "command": "echo hi",
        "cwd": str(tmp_path),
        "env": {},
        "allocate_ports": [],
        "pre": [],
        "post": [],
        "steps": [
            {"index": 0, "kind": "start",
             "assert": {"contains": ["NOT THERE"], "timeout_seconds": 0}},
        ],
    }))
    rc = main([str(plan_path), "--output-dir", str(tmp_path / "reports")])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_cli_returns_two_on_missing_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(replay_mod, "tmux_is_installed", lambda: True)
    rc = main([str(tmp_path / "does-not-exist.plan.yaml")])
    assert rc == 2
