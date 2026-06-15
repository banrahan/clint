"""Per-scenario port allocation.

The CLIs under test (e.g. ``azd ai agent run`` / ``invoke --local``) all
default to the same port (8088). When running scenarios in parallel — or
pairing ``run`` and ``invoke`` within one scenario — they collide.

``PortPool`` reserves free OS-assigned TCP ports and exposes them as
template variables so scenarios can write::

    allocate_ports: [agent]
    command: "azd ai agent run --port {agent}"

A pool is keyed by scenario path so two concurrent ``start_session``
calls referencing the same scenario share the same ``{agent}`` port
(letting ``run`` and ``invoke`` find each other).

Allocation strategy: bind ``AF_INET`` socket to ``('127.0.0.1', 0)``,
read the kernel-assigned port, close the socket. There is a small race
between us closing and the CLI binding, but in practice the kernel does
not hand out the same port to two consecutive ``bind(0)`` calls within
the same process, and the CLI will grab it within milliseconds.
"""

from __future__ import annotations

import socket
import threading
from typing import Iterable, Mapping

# Module-level registry of scenario_path → PortPool. Guarded by ``_REGISTRY_LOCK``
# so concurrent ``start_session`` calls don't race on first allocation.
_REGISTRY: dict[str, "PortPool"] = {}
_REGISTRY_LOCK = threading.Lock()


def allocate_free_port() -> int:
    """Return a TCP port the kernel believes is currently free.

    Closes the probe socket before returning, so the port is immediately
    available for another bind.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortPool:
    """A named set of ports shared across sessions of one scenario.

    Ports are allocated lazily on first ``get()``. Subsequent ``get()``
    calls return the same value for the same name.

    Pool specs:

    * ``names``: list of string names (``["agent", "proxy"]``) → exposed
      as ``{agent}`` / ``{proxy}``.
    * ``count``: integer N → exposed as ``{port1}`` .. ``{portN}`` and
      ``{port}`` aliases ``{port1}``.
    """

    def __init__(
        self,
        names: Iterable[str] | None = None,
        count: int = 0,
    ) -> None:
        spec: list[str] = []
        if names:
            spec.extend(names)
        if count > 0:
            for i in range(1, count + 1):
                spec.append(f"port{i}")
        # Dedupe while preserving order.
        seen: set[str] = set()
        self._names: list[str] = []
        for n in spec:
            if n in seen:
                continue
            seen.add(n)
            self._names.append(n)

        self._ports: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def get(self, name: str) -> int:
        """Return the port for ``name``, allocating if needed."""
        if name not in self._names:
            raise KeyError(
                f"Port '{name}' was not declared in this pool; "
                f"declared: {self._names or 'none'}"
            )
        with self._lock:
            if name not in self._ports:
                self._ports[name] = allocate_free_port()
            return self._ports[name]

    def vars(self) -> dict[str, int]:
        """Return a template-var dict with all declared ports allocated.

        ``{port}`` is included as an alias for ``{port1}`` when a numbered
        pool was requested.
        """
        result: dict[str, int] = {name: self.get(name) for name in self._names}
        if "port1" in result and "port" not in result:
            result["port"] = result["port1"]
        return result

    def reset(self) -> None:
        """Forget allocated ports. Used in tests; not part of public API."""
        with self._lock:
            self._ports.clear()


def parse_allocate_ports(raw: object) -> PortPool:
    """Parse the YAML ``allocate_ports`` field into a fresh ``PortPool``.

    Accepted forms:

    * ``None`` / missing → empty pool
    * ``int`` (``allocate_ports: 2``) → numbered pool of size 2
    * ``list[str]`` (``allocate_ports: [agent, proxy]``) → named pool
    """
    if raw is None:
        return PortPool()
    if isinstance(raw, bool):
        # ``allocate_ports: true`` is a friendly shorthand for "give me one port".
        return PortPool(count=1) if raw else PortPool()
    if isinstance(raw, int):
        if raw < 0:
            raise ValueError(f"allocate_ports must be >= 0, got {raw}")
        return PortPool(count=raw)
    if isinstance(raw, list):
        names: list[str] = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, str) or not entry.isidentifier():
                raise ValueError(
                    f"allocate_ports[{i}] must be a valid identifier string, got {entry!r}"
                )
            names.append(entry)
        return PortPool(names=names)
    raise ValueError(
        f"allocate_ports must be int, list[str], or bool; got {type(raw).__name__}"
    )


def get_pool(scenario_path: str, spec: object) -> PortPool:
    """Get-or-create the shared ``PortPool`` for ``scenario_path``.

    The first call materialises the pool from ``spec`` (the raw YAML
    value). Subsequent calls return the same pool and ignore ``spec``
    so that ``load_scenario`` and multiple ``start_session`` calls all
    see the same allocated ports.
    """
    with _REGISTRY_LOCK:
        pool = _REGISTRY.get(scenario_path)
        if pool is None:
            pool = parse_allocate_ports(spec)
            _REGISTRY[scenario_path] = pool
        return pool


def release_pool(scenario_path: str) -> bool:
    """Drop the pool for ``scenario_path``. Returns ``True`` if one existed."""
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(scenario_path, None) is not None


def reset_registry() -> None:
    """Test-only: clear all registered pools."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def merged_vars(
    pool: PortPool, extra: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Combine pool ports with caller-supplied vars (extra wins on conflict)."""
    out: dict[str, object] = dict(pool.vars()) if pool.names else {}
    if extra:
        out.update(extra)
    return out
