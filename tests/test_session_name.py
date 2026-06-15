from clint.agent import AgentSession
from clint.runner import tmux_session_name, SESSION_PREFIX


def test_tmux_name_unique_per_call_without_suffix():
    a = tmux_session_name()
    b = tmux_session_name()
    assert a != b
    assert a.startswith(SESSION_PREFIX)
    assert b.startswith(SESSION_PREFIX)


def test_tmux_name_uses_suffix_when_provided():
    a = tmux_session_name("alpha")
    assert a.endswith("-alpha")
    b = tmux_session_name("beta")
    assert a != b


def test_tmux_name_sanitises_unsafe_chars():
    out = tmux_session_name("has spaces:and.dots")
    for bad in (" ", ":", "."):
        assert bad not in out


def test_two_agent_sessions_get_distinct_tmux_names():
    """Two AgentSession instances in one process must pick different
    session_names. Previously they collided on PID."""
    s1 = AgentSession(command="echo a", session_id="s1")
    s2 = AgentSession(command="echo b", session_id="s2")
    assert s1.session_name != s2.session_name


def test_session_name_includes_session_id_for_traceability():
    s = AgentSession(command="echo x", session_id="run-agent")
    assert "run-agent" in s.session_name
