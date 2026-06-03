"""Simple ``{name}`` template substitution for scenario fields.

Used by ``mcp_server`` to inject port allocations (and other per-run
values) into scenario ``command``, ``cwd``, ``goals``, hook ``run`` /
``cwd`` / ``env`` strings before they are handed to ``AgentSession`` or
``execute_hooks``.

Design choices:

* Only ``{name}`` placeholders are recognised. Names must be a valid
  Python identifier so we don't accidentally consume JSON / shell braces.
* ``{{`` and ``}}`` are literal ``{`` and ``}`` (mirrors ``str.format``)
  so scenarios that legitimately contain braces can opt out.
* Unknown placeholders raise ``KeyError`` with a clear message rather
  than silently leaving the literal text — early failure beats a CLI
  invoked with a literal ``{port}`` in argv.
* Anything that *doesn't* look like a placeholder (e.g. ``{foo bar}``,
  ``{ }``, JSON like ``{"k": 1}``) is left untouched. This keeps the
  helper safe to apply to goal prose and hook ``run`` strings that may
  embed shell or JSON.
"""

from __future__ import annotations

import re
from typing import Mapping

_PLACEHOLDER_RE = re.compile(
    r"""
    \{\{                # escaped literal '{{'
    | \}\}              # escaped literal '}}'
    | \{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}
    """,
    re.VERBOSE,
)


def substitute_template(s: str, vars: Mapping[str, object]) -> str:
    """Replace ``{name}`` placeholders in ``s`` using ``vars``.

    Raises ``KeyError`` if a placeholder references a name not in ``vars``.
    Non-placeholder braces are left as-is.
    """
    if not isinstance(s, str):
        return s

    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = m.group("name")
        if name not in vars:
            raise KeyError(
                f"Unknown placeholder {{{name}}} in template; "
                f"available: {sorted(vars.keys()) or 'none'}"
            )
        return str(vars[name])

    return _PLACEHOLDER_RE.sub(repl, s)


def substitute_in_mapping(
    m: Mapping[str, str], vars: Mapping[str, object]
) -> dict[str, str]:
    """Apply ``substitute_template`` to every value in a string→string mapping."""
    return {k: substitute_template(v, vars) for k, v in m.items()}
