"""Memory de-duplication, and the correction it used to swallow.

The store exists to capture corrections. Substring matching made a correction
look like a duplicate of the mistake it corrected — "use Edge" is contained in
"do not use Edge" — so the correction was silently dropped and JARVIS repeated
the error forever. These tests exist so that cannot come back.
"""

from __future__ import annotations

import pytest

from jarvis.core import memory as memory_module
from jarvis.core.memory import MemoryStore
from jarvis.core.text import normalise, polarity


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "MEMORY_FILE", tmp_path / "memories.jsonl")
    return MemoryStore()


# ---- the bug ---------------------------------------------------------------


def test_a_correction_is_not_swallowed_by_what_it_corrects(store):
    store.add("use Edge for videos", kind="preference")
    store.add("do not use Edge for videos", kind="preference")

    contents = [m.content for m in store.all()]
    assert "do not use Edge for videos" in contents
    assert len(contents) == 2


@pytest.mark.parametrize("first,second", [
    ("open links in Chrome", "never open links in Chrome"),
    ("the work drive is E:", "the work drive is not E:"),
    ("run backups nightly", "don't run backups nightly"),
    ("close Slack on startup", "avoid closing Slack on startup"),
])
def test_opposites_are_both_kept(store, first, second):
    store.add(first, kind="preference")
    store.add(second, kind="preference")
    assert len(store.all()) == 2


# ---- de-duplication that should still happen -------------------------------


def test_exact_repeats_collapse(store):
    a = store.add("keep replies short", kind="preference")
    b = store.add("keep replies short", kind="preference")
    assert a.id == b.id
    assert len(store.all()) == 1


def test_punctuation_and_spacing_do_not_defeat_dedup(store):
    store.add("keep replies short", kind="preference")
    store.add("  Keep   replies, short!  ", kind="preference")
    assert len(store.all()) == 1


def test_a_vaguer_restatement_does_not_displace_a_specific_one(store):
    store.add("open videos in Firefox on the second monitor", kind="preference")
    store.add("open videos in Firefox", kind="preference")
    assert len(store.all()) == 1
    assert store.all()[0].content == "open videos in Firefox on the second monitor"


def test_adding_a_prohibition_makes_it_a_different_statement(store):
    """"...never Edge" says something the bare preference does not.

    Different polarity means these are not restatements of each other, so both
    are kept. Erring toward keeping information is the right way to be wrong
    here: a redundant line in the prompt costs a few tokens, a discarded
    prohibition costs the behaviour it was meant to prevent.
    """
    store.add("open videos in Firefox, never Edge", kind="preference")
    store.add("open videos in Firefox", kind="preference")
    assert len(store.all()) == 2


def test_a_more_specific_restatement_replaces_the_vaguer_one(store):
    store.add("open videos in Firefox", kind="preference")
    store.add("open videos in Firefox on the second monitor", kind="preference")
    assert len(store.all()) == 1
    assert store.all()[0].content == "open videos in Firefox on the second monitor"


def test_kinds_do_not_collide(store):
    """The same sentence as a fact and as a preference are different memories."""
    store.add("the work drive is E:", kind="fact")
    store.add("the work drive is E:", kind="preference")
    assert len(store.all()) == 2


def test_forget_removes_only_the_named_memory(store):
    a = store.add("first thing", kind="fact")
    store.add("second thing", kind="fact")

    assert store.forget(a.id) is True
    assert [m.content for m in store.all()] == ["second thing"]
    assert store.forget("nonexistent") is False


# ---- the prompt block ------------------------------------------------------


def test_prompt_block_carries_preferences_and_corrections_not_facts(store):
    store.add("keep replies short", kind="preference")
    store.add("YouTube is a website, not an app", kind="correction")
    store.add("my sister is called Mara", kind="fact")

    block = store.prompt_block()

    assert "keep replies short" in block
    assert "YouTube is a website" in block
    assert "Mara" not in block  # facts are recalled on demand, not injected


def test_prompt_block_is_empty_with_nothing_learned(store):
    assert store.prompt_block() == ""


# ---- the shared text helpers ----------------------------------------------


def test_normalise_strips_punctuation_and_case():
    assert normalise("  Use  Edge, always! ") == "use edge always"


def test_polarity_detects_negation_regardless_of_apostrophe():
    assert polarity("don't do it") == polarity("dont do it")
    assert polarity("do it") != polarity("don't do it")
