"""The entity-resolution model: a RapidFuzz scorer over a local alias index.

The index is loaded once at startup (see ``lifespan`` in ``main.py``) and reused
for every request. ``resolve`` is synchronous and CPU-bound, so async callers
must run it off the event loop (``asyncio.to_thread``) or from a plain ``def``.
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|plc|"
    r"group|holding|holdings|stores)\b\.?",
    re.IGNORECASE,
)


def normalize(name: str) -> str:
    """Lowercase, drop legal suffixes and punctuation, collapse whitespace."""
    name = _LEGAL_SUFFIXES.sub(" ", name.lower())
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


@dataclass(frozen=True)
class Entity:
    wikidata_id: str
    canonical_name: str
    parent_company: str | None
    headquarters: str | None


@dataclass(frozen=True)
class Match:
    entity: Entity
    score: float  # 0.0 - 1.0


class Resolver:
    """Fuzzy-matches a company name against every known alias."""

    def __init__(
        self, index_path: Path, threshold: float = 0.8, delay_seconds: float = 0.0
    ) -> None:
        records = json.loads(index_path.read_text(encoding="utf-8"))
        self._threshold = threshold
        self._delay = delay_seconds
        self._choices: list[str] = []
        self._owners: list[Entity] = []
        for rec in records:
            entity = Entity(
                wikidata_id=rec["wikidata_id"],
                canonical_name=rec["canonical_name"],
                parent_company=rec.get("parent_company"),
                headquarters=rec.get("headquarters"),
            )
            for alias in rec["aliases"]:
                self._choices.append(normalize(alias))
                self._owners.append(entity)

    def resolve(self, name: str) -> Match | None:
        """Return the best match above the threshold, or None (blocking call)."""
        if self._delay:
            time.sleep(self._delay)  # stand-in for a slow model; see discussion.md
        best = process.extractOne(normalize(name), self._choices, scorer=fuzz.WRatio)
        if best is None:
            return None
        _, score, idx = best
        if score / 100 < self._threshold:
            return None
        return Match(entity=self._owners[idx], score=round(score / 100, 3))
