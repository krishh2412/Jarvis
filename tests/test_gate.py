"""The safety gate: what runs, what stops, and what has to be asked about.

These are the tests worth having. Everything else in this project fails by
being unhelpful; the gate fails by letting an 8B model shut down the machine or
wipe a drive, so its decision table is the one thing that should not be able to
regress quietly.
"""

from __future__ import annotations

import pytest

from jarvis.safety import gate
from jarvis.tools import registry


# ---- layer 1: the hard denylist -------------------------------------------


@pytest.mark.parametrize("command", [
    "format C:",
    "FORMAT c: /q",
    "diskpart",
    "vssadmin delete shadows /all",
    "reg delete HKLM\\SOFTWARE\\Microsoft",
    "bcdedit /set testsigning on",
    "cipher /w:C",
])
@pytest.mark.parametrize("safety_level", ["full", "standard", "strict"])
def test_catastrophic_commands_blocked_at_every_level(command, safety_level, level):
    """Layer 1 outranks the level setting — even "full" cannot authorise these."""
    level(safety_level)
    decision, reason = gate.check("run_command", {"command": command})
    assert decision == "block"
    assert "denylist" in reason


def test_denylist_applies_to_powershell_too(level):
    level("full")
    decision, _ = gate.check("run_powershell", {"command": "diskpart"})
    assert decision == "block"


def test_ordinary_commands_are_not_denylisted(level):
    level("full")
    decision, _ = gate.check("run_command", {"command": "echo hello"})
    assert decision == "allow"


# ---- layer 2 + 3: classification against the level -------------------------


def test_full_level_waves_everything_through(level):
    level("full")
    assert gate.check("power_action", {"action": "shutdown"})[0] == "allow"
    assert gate.check("delete_path", {"path": "C:/tmp/x"})[0] == "allow"


def test_standard_confirms_irreversible_only(level):
    level("standard")
    assert gate.check("power_action", {"action": "shutdown"})[0] == "confirm"
    assert gate.check("run_command", {"command": "echo hi"})[0] == "confirm"
    assert gate.check("kill_process", {"name_or_pid": "chrome"})[0] == "confirm"
    # Reversible: journaled and undoable, so it passes silently.
    assert gate.check("delete_path", {"path": "C:/tmp/x"})[0] == "allow"
    assert gate.check("write_file", {"path": "C:/tmp/x", "content": ""})[0] == "allow"


def test_strict_confirms_anything_destructive(level):
    level("strict")
    assert gate.check("delete_path", {"path": "C:/tmp/x"})[0] == "confirm"
    assert gate.check("power_action", {"action": "shutdown"})[0] == "confirm"
    # Read-only tools are never confirmed, at any level.
    assert gate.check("system_stats", {})[0] == "allow"
    assert gate.check("read_file", {"path": "C:/tmp/x"})[0] == "allow"


def test_lock_and_sleep_are_reversible_power_actions(level):
    """Locking the screen is not in the same class as pulling the plug."""
    level("standard")
    assert gate.check("power_action", {"action": "lock"})[0] == "allow"
    assert gate.check("power_action", {"action": "sleep"})[0] == "allow"
    assert gate.check("power_action", {"action": "restart"})[0] == "confirm"


def test_unknown_tools_are_treated_as_safe(level):
    """An unclassified tool is a read, not a bomb — it has no entry in either set."""
    level("strict")
    assert gate.check("some_new_read_only_tool", {})[0] == "allow"


# ---- study mode sandbox ----------------------------------------------------


def test_study_mode_blocks_writes_outside_the_project(level):
    level("full")  # even unrestricted, study mode holds
    gate.enter_study()
    decision, reason = gate.check("delete_path", {"path": "C:/tmp/x"})
    assert decision == "block"
    assert "study mode" in reason


def test_study_mode_allows_its_own_writes_and_all_reads(level):
    level("full")
    gate.enter_study()
    assert gate.check("save_skill", {"name": "x"})[0] == "allow"
    assert gate.check("remember", {"content": "x"})[0] == "allow"
    assert gate.check("system_stats", {})[0] == "allow"


def test_leaving_study_mode_restores_normal_policy(level):
    level("full")
    gate.enter_study()
    gate.exit_study()
    assert gate.check("delete_path", {"path": "C:/tmp/x"})[0] == "allow"


# ---- approval: the human is the only one who can say yes -------------------


def test_no_approver_means_no(level):
    """A confirmable action with nobody to ask is refused, not allowed."""
    gate.set_approver(None)
    approved, reason = gate.ask("power_action", {"action": "shutdown"},
                                "shutdown the PC")
    assert approved is False
    assert "no interface" in reason


def test_approver_grant_and_refusal():
    gate.set_approver(lambda tool, desc, args: True)
    assert gate.ask("power_action", {}, "shutdown the PC")[0] is True

    gate.set_approver(lambda tool, desc, args: False)
    approved, reason = gate.ask("power_action", {}, "shutdown the PC")
    assert approved is False
    assert "declined" in reason


def test_broken_approver_is_a_refusal():
    """An approver that raises must not fall through to "yes"."""
    def explode(tool, desc, args):
        raise RuntimeError("window is gone")

    gate.set_approver(explode)
    approved, reason = gate.ask("power_action", {}, "shutdown the PC")
    assert approved is False
    assert "RuntimeError" in reason


def test_approver_sees_what_it_is_approving():
    seen = {}
    gate.set_approver(lambda tool, desc, args: seen.update(
        tool=tool, desc=desc, args=args) or True)
    gate.ask("run_command", {"command": "echo hi"}, "run this command: echo hi")
    assert seen["tool"] == "run_command"
    assert seen["args"] == {"command": "echo hi"}
    assert "echo hi" in seen["desc"]


def test_approver_cannot_mutate_the_call_it_was_shown():
    """The approver gets a copy; tampering with it must not change what runs."""
    def tamper(tool, desc, args):
        args["command"] = "format C:"
        return True

    original = {"command": "echo hi"}
    gate.set_approver(tamper)
    gate.ask("run_command", original, "run this command: echo hi")
    assert original == {"command": "echo hi"}


# ---- the model has no route to self-approval -------------------------------


def test_no_model_facing_tool_can_approve_an_action():
    """The regression guard for the whole redesign.

    Approval used to be a registered tool holding a token the model was handed
    in a tool result. Nothing in the registry may offer that again.
    """
    registry.load_all()
    for name, entry in registry.REGISTRY.items():
        blurb = f"{name} {entry.description}".lower()
        assert "confirm_action" not in name
        assert not ("approve" in blurb and "token" in blurb), (
            f"{name} looks like a self-approval route: {entry.description}"
        )


def test_denied_call_does_not_execute(level, tmp_path):
    """End to end: a refusal must leave the filesystem untouched."""
    level("strict")
    target = tmp_path / "keep.txt"
    target.write_text("original", encoding="utf-8")

    registry.load_all()
    gate.set_approver(lambda tool, desc, args: False)
    result = registry.execute("write_file",
                              {"path": str(target), "content": "clobbered"})

    assert result.get("denied") is True
    assert target.read_text(encoding="utf-8") == "original"


def test_approved_call_does_execute(level, tmp_path):
    level("strict")
    target = tmp_path / "write.txt"

    registry.load_all()
    gate.set_approver(lambda tool, desc, args: True)
    result = registry.execute("write_file",
                              {"path": str(target), "content": "written"})

    assert "error" not in result
    assert target.read_text(encoding="utf-8") == "written"


def test_broken_gate_refuses_destructive_tools_but_allows_reads(monkeypatch, tmp_path):
    """Fail closed: a gate that raises must not become a gate that waves through."""
    def explode(tool, args):
        raise RuntimeError("bad regex in config.json")

    registry.load_all()
    monkeypatch.setattr(gate, "check", explode)

    target = tmp_path / "keep.txt"
    target.write_text("original", encoding="utf-8")
    result = registry.execute("write_file",
                              {"path": str(target), "content": "clobbered"})
    assert result.get("blocked") is True
    assert target.read_text(encoding="utf-8") == "original"

    # Reads still work, so a broken gate degrades to read-only rather than dead.
    assert "iso" in registry.execute("get_datetime", {})
