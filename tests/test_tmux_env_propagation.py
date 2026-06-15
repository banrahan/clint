"""Regression test: scenario-provided env vars must be passed to tmux via
``-e KEY=VAL`` so they override the tmux server's frozen environment.

Without ``-e``, vars like ``HOME`` silently no-op because a long-running
tmux server pins its env at startup and new sessions inherit *server*
env, not the env of the ``tmux new-session`` caller. This was discovered
when a Copilot CLI "clean install" scenario kept writing to the real
``~/.copilot/`` despite an ``env: HOME: /tmp/...`` override.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from clint.runner import tmux_create_session


def _captured_argv(env: dict[str, str]) -> list[str]:
    with patch("clint.runner.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        tmux_create_session("s1", "bash", "/tmp", env)
        ((argv,), _kwargs) = mock_run.call_args
        return list(argv)


def test_scenario_env_passed_via_dash_e() -> None:
    argv = _captured_argv({"HOME": "/tmp/fake-home", "MY_VAR": "x"})
    assert "-e" in argv
    assert "HOME=/tmp/fake-home" in argv
    assert "MY_VAR=x" in argv


def test_force_color_and_term_defaults_also_passed_via_dash_e() -> None:
    argv = _captured_argv({})
    assert "FORCE_COLOR=1" in argv
    assert "TERM=xterm-256color" in argv


def test_caller_overrides_force_color_default() -> None:
    argv = _captured_argv({"FORCE_COLOR": "0"})
    assert "FORCE_COLOR=0" in argv
    assert "FORCE_COLOR=1" not in argv


def test_dash_e_appears_before_command() -> None:
    argv = _captured_argv({"HOME": "/tmp/fake-home"})
    home_idx = argv.index("HOME=/tmp/fake-home")
    cmd_idx = argv.index("bash")
    assert home_idx < cmd_idx, "env vars must precede the command argument"
