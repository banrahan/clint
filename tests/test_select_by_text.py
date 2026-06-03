"""Hermetic tests for select_by_text.

These tests monkeypatch the three tmux primitives in
``auto_test_tool.agent`` so they can drive the real ``_select_by_text``
implementation against scripted pane captures without touching tmux.
"""
from __future__ import annotations

import pytest

from auto_test_tool import agent as agent_mod


class FakeTmux:
    """Stand-in for the (capture, send_keys, send_text) trio.

    A test supplies a sequence of pane snapshots that are returned in order
    by ``capture`` (the last one is sticky). All ``send_keys`` / ``send_text``
    calls are appended to ``self.events`` so the test can assert on them.
    """

    def __init__(self, captures: list[str]) -> None:
        self._captures = list(captures)
        self.events: list[tuple[str, str]] = []  # (kind, payload)
        # Tests can mutate this to react to send_keys/send_text dynamically.
        self.on_event = None  # type: ignore[assignment]

    def capture(self, session: str, with_ansi: bool = False) -> str:  # noqa: ARG002
        if len(self._captures) > 1:
            return self._captures.pop(0)
        return self._captures[0]

    def send_keys(self, session: str, key: str) -> None:  # noqa: ARG002
        self.events.append(("key", key))
        if self.on_event:
            self.on_event(self, "key", key)

    def send_text(self, session: str, text: str) -> None:  # noqa: ARG002
        self.events.append(("text", text))
        if self.on_event:
            self.on_event(self, "text", text)


@pytest.fixture
def patch_tmux(monkeypatch):
    """Install a FakeTmux and return it. Also no-ops time.sleep for speed."""

    def install(captures: list[str]) -> FakeTmux:
        fake = FakeTmux(captures)
        monkeypatch.setattr(agent_mod, "tmux_capture_pane", fake.capture)
        monkeypatch.setattr(agent_mod, "tmux_send_keys", fake.send_keys)
        monkeypatch.setattr(agent_mod, "tmux_send_text", fake.send_text)
        monkeypatch.setattr(agent_mod.time, "sleep", lambda *_a, **_kw: None)
        return fake

    return install


def _enter_events(events):
    return [e for e in events if e == ("key", "Enter")]


def _keys(events, key):
    return [e for e in events if e == ("key", key)]


def test_picks_when_highlighted_on_first_capture(patch_tmux):
    """If the target is already on a highlighted line, no typing — just Enter."""
    pane = "\n".join([
        "? Select language:",
        "> Python",
        "  C#",
    ])
    fake = patch_tmux([pane])
    agent_mod._select_by_text("sess", "Python")
    # Exactly one Enter, nothing else.
    assert fake.events == [("key", "Enter")]


def test_filter_typing_when_not_highlighted(patch_tmux):
    """Filter the list by typing the target, then Enter when it lands."""
    before = "\n".join([
        "? Select sub:",
        "> Picasso DevX",
        "  Engineering Hub Dev",
    ])
    after_filter = "\n".join([
        "? Select sub:",
        "> benhanrahan subscription",
    ])
    fake = patch_tmux([before, after_filter])
    agent_mod._select_by_text("sess", "benhanrahan")
    # First a text-typed filter, then an Enter.
    assert ("text", "benhanrahan") in fake.events
    assert _enter_events(fake.events) == [("key", "Enter")]
    # No scroll-fallback should have been needed.
    assert _keys(fake.events, "Down") == []


def test_offscreen_target_filtered_into_view(patch_tmux):
    """Target absent in initial capture, present in post-filter capture."""
    initial = "\n".join([
        "? Select sub:",
        "> Subscription One",
        "  Subscription Two",
        "  Subscription Three",
    ])
    filtered = "\n".join([
        "? Select sub:",
        "> benhanrahan subscription",
    ])
    fake = patch_tmux([initial, filtered])
    agent_mod._select_by_text("sess", "benhanrahan")
    assert ("text", "benhanrahan") in fake.events
    assert _enter_events(fake.events) == [("key", "Enter")]


def test_absent_target_raises_lookup_error(patch_tmux):
    """Neither initial pane nor filter typing reveal the target → raise."""
    initial = "\n".join([
        "? Select sub:",
        "> Subscription One",
        "  Subscription Two",
    ])
    # Same capture re-used after filter typing AND for the scroll fallback.
    fake = patch_tmux([initial])
    with pytest.raises(LookupError) as exc:
        agent_mod._select_by_text("sess", "benhanrahan")
    assert "benhanrahan" in str(exc.value)
    # Critically: NO Enter must have been sent.
    assert _enter_events(fake.events) == []


def test_multiple_visible_matches_prefers_highlighted(patch_tmux):
    """Two lines contain the target; only the highlighted one wins."""
    pane = "\n".join([
        "? Select project:",
        "  benhanrahan dev",
        "> benhanrahan prod",
        "  Other project",
    ])
    fake = patch_tmux([pane])
    agent_mod._select_by_text("sess", "benhanrahan prod")
    # Phase 1 highlighted match → straight to Enter, no typing.
    assert fake.events == [("key", "Enter")]


def test_scroll_fallback_finds_item_after_clearing_filter(patch_tmux):
    """If filter typing doesn't help, clear it (BSpace * len) and scroll."""
    # Sequence of captures consumed by _select_by_text:
    #   0: phase 1 — target offscreen, not highlighted
    #   1: phase 2 (after filter typing) — picker doesn't support filter, unchanged
    #   2: phase 3 (after BSpaces) — still no match, will press Down
    #   3+: after each Down — eventually the target gets highlighted (sticky)
    initial = "\n".join(["? Pick:", "> One", "  Two", "  Three"])
    after_scroll = "\n".join(["? Pick:", "  Two", "  Three", "> benhanrahan"])
    fake = patch_tmux([initial, initial, initial, after_scroll])
    agent_mod._select_by_text("sess", "benhanrahan")
    # Must have cleared the filter — one BSpace per char of "benhanrahan".
    bspaces = _keys(fake.events, "BSpace")
    assert len(bspaces) == len("benhanrahan")
    # Must have used at least one Down then Entered.
    assert _keys(fake.events, "Down")
    assert _enter_events(fake.events) == [("key", "Enter")]


def test_empty_target_rejected_at_action_layer():
    """The dispatcher refuses an empty select_by_text payload."""
    with pytest.raises(ValueError):
        agent_mod._execute_agent_action("sess", {"action": "select_by_text", "text": ""})
