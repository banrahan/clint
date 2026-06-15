import socket

import pytest

from clint.ports import (
    PortPool,
    allocate_free_port,
    get_pool,
    parse_allocate_ports,
    release_pool,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


def test_allocate_free_port_returns_usable_port():
    p = allocate_free_port()
    assert 1024 < p < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", p))


def test_named_pool_lazy_allocation():
    pool = PortPool(names=["agent", "proxy"])
    assert pool.names == ["agent", "proxy"]
    a1 = pool.get("agent")
    a2 = pool.get("agent")
    assert a1 == a2
    assert pool.get("proxy") != a1


def test_numbered_pool_vars_include_port_alias():
    pool = PortPool(count=2)
    v = pool.vars()
    assert set(v.keys()) == {"port1", "port2", "port"}
    assert v["port"] == v["port1"]
    assert v["port1"] != v["port2"]


def test_undeclared_name_raises():
    pool = PortPool(count=1)
    with pytest.raises(KeyError):
        pool.get("not_declared")


def test_parse_allocate_ports_forms():
    assert parse_allocate_ports(None).names == []
    assert parse_allocate_ports(True).names == ["port1"]
    assert parse_allocate_ports(False).names == []
    assert parse_allocate_ports(2).names == ["port1", "port2"]
    assert parse_allocate_ports(["a", "b"]).names == ["a", "b"]


def test_parse_allocate_ports_rejects_invalid():
    with pytest.raises(ValueError):
        parse_allocate_ports(-1)
    with pytest.raises(ValueError):
        parse_allocate_ports([1, 2])
    with pytest.raises(ValueError):
        parse_allocate_ports(["valid", "not valid name"])
    with pytest.raises(ValueError):
        parse_allocate_ports("oops")


def test_get_pool_shared_across_calls_same_path():
    pool_a = get_pool("/tmp/scenarioA.yaml", ["agent"])
    pool_b = get_pool("/tmp/scenarioA.yaml", ["agent"])
    assert pool_a is pool_b
    assert pool_a.get("agent") == pool_b.get("agent")


def test_get_pool_distinct_for_different_paths():
    pa = get_pool("/tmp/A.yaml", ["agent"])
    pb = get_pool("/tmp/B.yaml", ["agent"])
    assert pa is not pb
    # Note: distinct PortPool objects allocate independently; we don't assert
    # the integer ports differ (the OS may reuse a freshly-closed port).


def test_release_pool_drops_registration():
    pool = get_pool("/tmp/release.yaml", 1)
    _ = pool.get("port1")
    assert release_pool("/tmp/release.yaml") is True
    assert release_pool("/tmp/release.yaml") is False
    new_pool = get_pool("/tmp/release.yaml", 1)
    assert new_pool is not pool


def test_subsequent_allocations_distinct_within_one_process():
    ports = {allocate_free_port() for _ in range(20)}
    assert len(ports) > 1
