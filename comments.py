"""Comment support score Q (0..1).

Evaluates whether written feedback actually substantiates the numeric
allocation, using text-completeness indicators plus repetition / cross-comment
similarity detection. Drives a Points column = round(Q * max_points).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, List

CONTRIBUTION_KEYWORDS = {
    "contribut", "led", "lead", "organiz", "wrote", "coded", "designed",
    "research", "present", "slide", "test", "debug", "meeting", "deadline",
    "helped", "coordinat", "manage", "review", "analy", "built", "created",
    "communicat", "responsib", "delivered", "supported", "collaborat",
}

_WORD = re.compile(r"[a-zA-Z']+")
_SENT = re.compile(r"[.!?]+")


@dataclass
class CommentScore:
    q: float
    points: int
    word_count: int
    char_count: int
    sentence_count: int
    unique_ratio: float
    keyword_count: int
    flags: List[str]


def _tokens(text: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def score_comment(
    text: str,
    others: Iterable[str] = (),
    max_points: int = 5,
) -> CommentScore:
    text = (text or "").strip()
    words = _tokens(text)
    wc = len(words)
    cc = len(text)
    sc = len([s for s in _SENT.split(text) if s.strip()])
    unique_ratio = (len(set(words)) / wc) if wc else 0.0
    kw = sum(1 for w in words for k in CONTRIBUTION_KEYWORDS if w.startswith(k))

    flags: List[str] = []
    checks = {
        "words>=25": wc >= 25,
        "chars>=150": cc >= 150,
        "sentences>=2": sc >= 2,
        "unique_ratio>=0.45": unique_ratio >= 0.45,
        "keywords>=2": kw >= 2,
    }
    passed = sum(1 for v in checks.values() if v)
    q = passed / len(checks)

    # Repetition penalty (same phrase repeated)
    if wc and unique_ratio < 0.35:
        q *= 0.7
        flags.append("high repetition")

    # Cross-comment similarity: copy/paste across teammates
    max_sim = 0.0
    for o in others:
        if not o or o.strip() == text:
            continue
        sim = SequenceMatcher(None, text, o.strip()).ratio()
        max_sim = max(max_sim, sim)
    if max_sim >= 0.85 and wc:
        q *= 0.6
        flags.append(f"duplicate of another comment ({max_sim:.0%})")

    if wc == 0:
        flags.append("no comment")

    q = max(0.0, min(1.0, q))
    for name, ok in checks.items():
        if not ok:
            flags.append(f"missing: {name}")

    return CommentScore(
        q=round(q, 3),
        points=round(q * max_points),
        word_count=wc,
        char_count=cc,
        sentence_count=sc,
        unique_ratio=round(unique_ratio, 3),
        keyword_count=kw,
        flags=flags,
    )
