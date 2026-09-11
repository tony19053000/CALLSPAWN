"""Deterministic near-duplicate detection over text (strategies and agent ownership).

Token-set overlap (Jaccard) over normalized words. Purely lexical, purely
code: the model never gets to decide whether two of its own proposals are the
same idea.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_WORD_RE = re.compile(r"[a-z0-9]+")

# Function words that carry no meaning for overlap purposes. Generic English
# only; nothing here refers to any mission domain.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "per",
        "that",
        "the",
        "then",
        "this",
        "to",
        "via",
        "with",
        "we",
        "will",
        "each",
        "all",
        "any",
        "one",
        "first",
        "based",
        "using",
        "use",
        "than",
        "not",
        "no",
    }
)


def tokens(*texts: str) -> frozenset[str]:
    """Normalized content tokens across ``texts``: lower-case, alphanumeric, no stopwords."""
    out: set[str] = set()
    for text in texts:
        for word in _WORD_RE.findall(text.lower()):
            if len(word) >= 3 and word not in STOPWORDS:
                out.add(_stem(word))
    return frozenset(out)


def _stem(word: str) -> str:
    """Tiny suffix stripper so plurals and simple inflections coincide.

    Applied twice so ``suppliers`` -> ``supplier`` -> ``suppli`` meets ``supplier``.
    """
    for _ in range(2):
        for suffix in ("ies", "ing", "s", "ed", "er"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)] + ("y" if suffix == "ies" else "")
                break
        else:
            break
    return word


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    return len(set_a & set_b) / len(union) if union else 0.0


def overlap(a: Iterable[str], b: Iterable[str]) -> float:
    """Overlap coefficient: intersection over the smaller set. Catches the case
    where one statement is a subset of another."""
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 1.0 if set_a == set_b else 0.0
    return len(set_a & set_b) / min(len(set_a), len(set_b))


def normalize_label(label: str) -> str:
    """Canonical form of a short label such as an objective axis."""
    return " ".join(sorted(tokens(label))) or label.strip().lower()
