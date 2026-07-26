"""Text comparison helpers shared by the memory store and the learning notes.

Both keep small collections of hand-written statements and both need the same
question answered: is this new line already recorded? Naive substring matching
gets that wrong in the one case that matters most — "use Edge" is a substring of
"do not use Edge", so a correction looks like a duplicate of the very mistake it
corrects and is silently discarded. Polarity-aware comparison lives here so both
stores answer it the same way.
"""

from __future__ import annotations

import re

# Words that flip the meaning of a statement. Two texts carrying different
# negations are opposites however much they overlap.
NEGATIONS = frozenset({
    "not", "no", "never", "none", "nothing", "nor", "neither",
    "dont", "doesnt", "didnt", "isnt", "arent", "wasnt", "werent",
    "cant", "cannot", "wont", "shouldnt", "avoid", "stop", "without",
})

_WORD_RE = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    """Lowercase word tokens, apostrophes stripped so "don't" meets "dont"."""
    return [w.replace("'", "") for w in _WORD_RE.findall(text.lower())]


def normalise(text: str) -> str:
    """Punctuation- and whitespace-insensitive form, for comparing two notes."""
    return " ".join(words(text))


def polarity(text: str) -> frozenset[str]:
    """The negation words present in `text`. Equal sets mean equal polarity."""
    return frozenset(w for w in words(text) if w in NEGATIONS)
