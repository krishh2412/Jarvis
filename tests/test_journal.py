"""The undo journal: the reason running without confirmations is survivable.

Full autonomy is only defensible because deletes and overwrites are recoverable.
If stash/undo stops round-tripping, the whole safety argument for the app goes
with it and nothing else would notice.
"""

from __future__ import annotations

from jarvis.safety import journal


# ---- files -----------------------------------------------------------------


def test_stash_removes_the_original_and_undo_puts_it_back(sandbox):
    target = sandbox / "notes.txt"
    target.write_text("important", encoding="utf-8")

    entry_id = journal.stash(target, action="delete")

    assert entry_id is not None
    assert not target.exists()

    result = journal.undo(entry_id)

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "important"


def test_undo_with_no_id_reverses_the_most_recent(sandbox):
    first = sandbox / "first.txt"
    second = sandbox / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")

    journal.stash(first)
    journal.stash(second)

    journal.undo()

    assert second.exists()
    assert not first.exists()


def test_stash_preserves_content_exactly(sandbox):
    """Including bytes that a text round-trip would mangle."""
    target = sandbox / "binary.dat"
    payload = bytes(range(256))
    target.write_bytes(payload)

    entry_id = journal.stash(target)
    journal.undo(entry_id)

    assert target.read_bytes() == payload


def test_stashing_a_missing_path_is_a_no_op(sandbox):
    assert journal.stash(sandbox / "nothing.txt") is None


# ---- directories -----------------------------------------------------------


def test_directories_round_trip_with_their_contents(sandbox):
    tree = sandbox / "project"
    (tree / "nested").mkdir(parents=True)
    (tree / "top.txt").write_text("top", encoding="utf-8")
    (tree / "nested" / "deep.txt").write_text("deep", encoding="utf-8")

    entry_id = journal.stash(tree)
    assert not tree.exists()

    journal.undo(entry_id)

    assert (tree / "top.txt").read_text(encoding="utf-8") == "top"
    assert (tree / "nested" / "deep.txt").read_text(encoding="utf-8") == "deep"


# ---- refusals and bookkeeping ----------------------------------------------


def test_undo_refuses_to_clobber_a_recreated_original(sandbox):
    target = sandbox / "notes.txt"
    target.write_text("original", encoding="utf-8")
    entry_id = journal.stash(target)

    target.write_text("something new I have since written", encoding="utf-8")
    result = journal.undo(entry_id)

    assert result["ok"] is False
    assert "refusing to clobber" in result["error"]
    assert target.read_text(encoding="utf-8") == "something new I have since written"


def test_an_entry_cannot_be_undone_twice(sandbox):
    target = sandbox / "notes.txt"
    target.write_text("x", encoding="utf-8")
    entry_id = journal.stash(target)

    assert journal.undo(entry_id)["ok"] is True
    assert journal.undo(entry_id)["ok"] is False


def test_undo_with_nothing_stashed_says_so(sandbox):
    result = journal.undo()
    assert result["ok"] is False
    assert result["error"] == "nothing to undo"


def test_history_is_newest_first(sandbox):
    for name in ("a.txt", "b.txt", "c.txt"):
        path = sandbox / name
        path.write_text(name, encoding="utf-8")
        journal.stash(path)

    entries = journal.history()

    assert [e["original"].split("\\")[-1].split("/")[-1] for e in entries] == [
        "c.txt", "b.txt", "a.txt"
    ]


def test_a_corrupt_journal_line_does_not_break_reading(sandbox):
    target = sandbox / "notes.txt"
    target.write_text("x", encoding="utf-8")
    journal.stash(target)

    with open(journal.JOURNAL_FILE, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")

    assert len(journal.history()) == 1


# ---- protected paths -------------------------------------------------------


def test_windows_directories_are_protected():
    assert journal.is_protected(r"C:\Windows\System32\kernel32.dll") is not None
    assert journal.is_protected(r"C:\Windows") is not None


def test_ordinary_paths_are_not_protected(sandbox):
    assert journal.is_protected(sandbox / "anything.txt") is None
