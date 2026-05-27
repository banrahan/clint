#!/usr/bin/env python3
"""
Drive azd ai agent init: pick Python → Basic Responses template → complete init.
"""
import time
from auto_test_tool.agent import AgentSession
import os

HOME = os.path.expanduser("~")
TEST_DIR = f"{HOME}/working/agents/auto-test-run"
OUTPUT_DIR = f"{HOME}/working/auto-test-tool/screenshots"


def active_prompt(state):
    """
    Return the last few non-empty lines — this is where the active prompt lives.
    Filters out the separator/hint lines to find the actual prompt.
    """
    lines = [l for l in state.split("\n") if l.strip()]
    # Take last 8 lines — that's where the active prompt + choices live
    return "\n".join(lines[-8:]).lower()


def wait_for(session, pattern, timeout=90):
    """Poll until pattern appears in the ACTIVE prompt area."""
    for _ in range(int(timeout / 2)):
        state = session.observe()
        if pattern.lower() in active_prompt(state):
            return state
        time.sleep(2)
    return session.observe()


def pstate(state, label):
    lines = [l for l in state.split("\n") if l.strip()]
    print(f"\n--- {label} ---")
    for line in lines[-10:]:
        print(f"  {line}")


def main():
    session = AgentSession(
        command="azd ai agent init",
        cwd=TEST_DIR,
        output_dir=OUTPUT_DIR,
    )

    print("🚀 Starting azd ai agent init...")
    state = session.start()

    # 1. Language selection (empty dir → skips init mode)
    print("⏳ Waiting for language selection...")
    state = wait_for(session, "select a language")
    pstate(state, "Language prompt")
    session.screenshot("Language selection")

    print("🎯 Selecting Python (index 0)...")
    state = session.act({"action": "select", "choice_index": 0})
    time.sleep(2)

    # 2. Template selection
    print("⏳ Waiting for template list...")
    state = wait_for(session, "select a")
    pstate(state, "Template list")
    session.screenshot("Template selection")

    # Look for "Hello World" basic responses — typically first
    print("🎯 Selecting first template...")
    state = session.act({"action": "select", "choice_index": 0})
    time.sleep(3)

    # 3. Now handle all remaining prompts sequentially
    # Each prompt: wait for it, act, move on
    prompt_sequence = [
        ("deploy your agent", "select", 0, "Deploy mode → Container"),
        ("foundry project", "select", 0, "Foundry project"),
        ("subscription", "select", 0, "Subscription"),
        # May get "no existing foundry" → falls through to location
        ("location", "select", 0, "Location"),
        ("model", "select", 0, "Model choice"),
        # Model version, SKU, capacity, name — these are inputs/selects
        ("version", "select", 0, "Model version"),
        ("sku", "select", 0, "Model SKU"),
        ("capacity", "input", "10", "Model capacity"),
        ("deployment name", "input", "", "Model deployment name (default)"),
        ("resources", "select", 0, "CPU/Memory resources"),
    ]

    for pattern, action_type, value, desc in prompt_sequence:
        print(f"\n⏳ Waiting for: {desc}...")
        state = wait_for(session, pattern, timeout=60)
        prompt = active_prompt(state)

        if pattern.lower() not in prompt:
            print(f"  ⚠️  Pattern '{pattern}' not found in active prompt, skipping...")
            pstate(state, f"Skipped: {desc}")
            continue

        session.screenshot(desc)
        print(f"🎯 {desc}")

        if action_type == "select":
            state = session.act({"action": "select", "choice_index": value})
        elif action_type == "confirm":
            state = session.act({"action": "confirm", "value": value})
        elif action_type == "input":
            state = session.act({"action": "input", "text": str(value)})

        time.sleep(2)

    # 4. Wait for completion
    print("\n⏳ Waiting for init to complete...")
    state = wait_for(session, "next:", timeout=60)
    pstate(state, "Final output")
    session.screenshot("Completed")

    report = session.finish()
    print(f"\n📄 Report: {report}")
    print(f"📂 Screenshots: {session.run_dir}")


if __name__ == "__main__":
    main()
