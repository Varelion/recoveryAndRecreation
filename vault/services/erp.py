"""Scores a user's ERP (erotic roleplay) language across stored messages.

    messages ──► per message: explicit hits, suggestive cluster, OOC markers
                     │
                     ▼
              points = Σ hits × weight ──► score = points per 100 messages

Suggestive words only count when SUGGESTIVE_CLUSTER of them share a message:
"she kissed his lips" is innocent alone, noisy only in clusters.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from vault.store import Store

# Explicit terms: strong signal alone. Terms of STEM_MIN+ letters also match
# longer words: "penetrat" -> "penetrate", "penetrating".
ERP_EXPLICIT = [
    "cock", "dick", "penis", "shaft", "erection", "aroused", "arousal",
    "pussy", "clit", "clitoris", "vagina", "labia", "folds",
    "nipple", "nipples", "breasts", "tits",
    "orgasm", "climax", "cum", "cumming", "ejaculat",
    "thrust", "thrusting", "penetrat", "grind", "grinding",
    "moan", "moaning", "whimper", "panting",
    "naked", "nude", "undress", "lingerie",
    "lick", "licking", "suck", "sucking", "straddle", "straddling",
    "fuck", "fucking", "sex", "intercourse", "foreplay",
]

# Suggestive terms: weak alone, meaningful in clusters.
ERP_SUGGESTIVE = [
    "kiss", "kissing", "caress", "fondle", "grope", "touching",
    "bedroom", "bed", "sheets", "hips", "thighs", "lips",
    "desire", "lust", "heat", "wet", "hard", "throbbing",
    "slowly", "deeper", "harder",
]

# OOC markers players use around ERP.
ERP_MARKERS = [
    "ftb", "fade to black", "fades to black", "erp", "nsfw",
    "lewd", "spicy", "18+", "timeskip", "*time skip*",
]

SUGGESTIVE_CLUSTER = 2
STEM_MIN = 8
PER_MESSAGES = 100


class Category(Enum):
    EXPLICIT = "explicit"
    SUGGESTIVE = "suggestive"
    MARKER = "marker"


WEIGHTS = {Category.EXPLICIT: 3, Category.SUGGESTIVE: 1, Category.MARKER: 2}


@dataclass
class Report:
    user_id: int
    messages: int = 0
    flagged: int = 0
    hits: dict[Category, Counter] = field(default_factory=lambda: {c: Counter() for c in Category})

    @property
    def points(self) -> int:
        return sum(WEIGHTS[c] * sum(n.values()) for c, n in self.hits.items())

    @property
    def score(self) -> float:
        if not self.messages:
            return 0.0

        return round(self.points * PER_MESSAGES / self.messages, 2)

    def summary(self) -> dict:
        return {
            "user_id": self.user_id,
            "messages": self.messages,
            "flagged": self.flagged,
            "points": self.points,
            f"score_per_{PER_MESSAGES}": self.score,
            "hits": {c.value: dict(n.most_common()) for c, n in self.hits.items()},
        }


class Match(Enum):
    WORD = "word"
    STEM = "stem"


def _pattern(term: str, match: Match) -> re.Pattern:
    """Whole-term match; "18+" and "*time skip*" have non-word edges, so no \\b."""
    tail = r"\w*" if match is Match.STEM and len(term) >= STEM_MIN else r"(?!\w)"
    return re.compile(rf"(?<!\w){re.escape(term)}{tail}", re.IGNORECASE)


_PATTERNS = {
    Category.EXPLICIT: [(t, _pattern(t, Match.STEM)) for t in ERP_EXPLICIT],
    Category.SUGGESTIVE: [(t, _pattern(t, Match.WORD)) for t in ERP_SUGGESTIVE],
    Category.MARKER: [(t, _pattern(t, Match.WORD)) for t in ERP_MARKERS],
}


def _matches(text: str, category: Category) -> Counter:
    found = Counter()
    for term, pattern in _PATTERNS[category]:
        count = len(pattern.findall(text))
        if count:
            found[term] = count

    return found


def scan(user_id: int, texts: list[str]) -> Report:
    """Tally hits over message texts."""
    report = Report(user_id, messages=len(texts))
    for text in texts:
        found = {c: _matches(text, c) for c in Category}

        # Lone suggestive words are noise.
        if len(found[Category.SUGGESTIVE]) < SUGGESTIVE_CLUSTER:
            found[Category.SUGGESTIVE] = Counter()

        if not any(found.values()):
            continue

        report.flagged += 1
        for category, counts in found.items():
            report.hits[category].update(counts)

    return report


async def score(store: Store, user: str) -> Report | None:
    """Report for a user id, username, global name or nickname; None if unknown."""
    user_id = await store.find_user(user)
    if user_id is None:
        return None

    return scan(user_id, await store.user_texts(user_id))
