"""Tests for ``instance_id``-keyed PortPool sharing.

When N parallel runs of the same scenario are launched, each should get
its own port pool — they're independent instances, not the run+invoke
pair that the default per-scenario sharing is designed for.
"""

from pathlib import Path

import pytest

from auto_test_tool import mcp_server, ports


@pytest.fixture(autouse=True)
def _clean_registry():
    ports.reset_registry()
    yield
    ports.reset_registry()


def _write(tmp_path: Path) -> str:
    p = tmp_path / "scen.yaml"
    p.write_text(
        """
name: parallel
allocate_ports: [agent]
command: "azd ai agent run --port {agent}"
"""
    )
    return str(p)


def test_no_instance_id_shares_pool(tmp_path):
    """Default behavior: run + invoke of one instance share the same port."""
    path = _write(tmp_path)
    v1, p1 = mcp_server._resolve_vars(path, None, None)
    v2, p2 = mcp_server._resolve_vars(path, None, None)
    assert p1 is p2
    assert v1["agent"] == v2["agent"]


def test_distinct_instance_ids_get_independent_pools(tmp_path):
    path = _write(tmp_path)
    v_a, pool_a = mcp_server._resolve_vars(path, None, "1")
    v_b, pool_b = mcp_server._resolve_vars(path, None, "2")
    v_c, pool_c = mcp_server._resolve_vars(path, None, "3")
    # Three distinct PortPool instances.
    assert pool_a is not pool_b
    assert pool_b is not pool_c
    assert pool_a is not pool_c
    # Each instance gets a distinct port (extremely unlikely collision
    # because all three pools allocate from the kernel within one process).
    ports_seen = {v_a["agent"], v_b["agent"], v_c["agent"]}
    assert len(ports_seen) == 3, f"Expected 3 distinct ports, got {ports_seen}"


def test_same_instance_id_shares_pool(tmp_path):
    """The point of sharing: a run session and an invoke session for the
    same instance must see the same {agent}."""
    path = _write(tmp_path)
    v_run, pool_run = mcp_server._resolve_vars(path, None, "1")
    v_invoke, pool_invoke = mcp_server._resolve_vars(path, None, "1")
    assert pool_run is pool_invoke
    assert v_run["agent"] == v_invoke["agent"]


def test_instance_id_exposed_as_template_var(tmp_path):
    """{instance} should be available so scenarios can write
    cwd: '/tmp/runs/{instance}'."""
    path = _write(tmp_path)
    v, _ = mcp_server._resolve_vars(path, None, "alpha")
    assert v["instance"] == "alpha"


def test_session_vars_can_override_instance(tmp_path):
    """Explicit session_vars wins over the auto-injected {instance}."""
    path = _write(tmp_path)
    v, _ = mcp_server._resolve_vars(path, {"instance": "custom"}, "auto")
    assert v["instance"] == "custom"


def test_pool_key_helper_matches_resolve_vars(tmp_path):
    """``_pool_key_for_session`` must produce the same key
    ``_resolve_vars`` used, so finish_session releases the right pool."""
    path = _write(tmp_path)
    _ = mcp_server._resolve_vars(path, None, "k")
    key = mcp_server._pool_key_for_session(path, "k")
    # The pool must be findable under that key.
    pool = ports.get_pool(key, ["agent"])
    # Should be the same pool object as the one _resolve_vars created.
    _, pool_via_resolve = mcp_server._resolve_vars(path, None, "k")
    assert pool is pool_via_resolve
