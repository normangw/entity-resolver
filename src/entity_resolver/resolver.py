"""The entity-resolution model: a RapidFuzz scorer over a local alias index.

The index is loaded once at startup (see ``lifespan`` in ``main.py``) and reused
for every request. ``resolve`` is synchronous and CPU-bound, so async callers
must run it off the event loop (``asyncio.to_thread``) or from a plain ``def``.
"""

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|plc|"
    r"group|holding|holdings|stores|"
    r"gmbh|ag|sa|spa|srl|nv|bv|pty|oyj|kk)\b\.?",
    re.IGNORECASE,
)
# CJK legal forms have no word boundaries, so they are removed as substrings.
# Longest first, so 股份有限公司 goes before 有限公司.
_CJK_SUFFIXES = re.compile("股份有限公司|有限责任公司|有限公司|株式会社")
_LEADING_ARTICLE = re.compile(r"^the\s+")
# Dots between single letters ("S.A.", "N.V.") collapse so they match as one token.
_DOTTED_ABBREV = re.compile(r"(?<=\b\w)\.(?=\w\b)")

# Regions a caller can restrict matching to. "world" means no restriction.
_EU = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI "
    "ES SE".split()
)
REGIONS: dict[str, frozenset[str] | None] = {
    "world": None,
    "us": frozenset({"US"}),
    "cn": frozenset({"CN"}),
    "gb": frozenset({"GB"}),
    "jp": frozenset({"JP"}),
    "eu": _EU,
}


def _fold_accents(text: str) -> str:
    """Drop accents from Latin letters (Nestlé -> Nestle), leaving CJK intact."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.category(ch) == "Mn" and out and out[-1].isascii():
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def normalize(name: str) -> str:
    """Canonicalize a company name for matching.

    NFKC, accent folding, lowercase, ``&`` -> ``and``, leading "the" and legal
    suffixes removed, punctuation dropped, whitespace collapsed. If stripping
    would leave nothing (e.g. the input "Group Inc."), the unstripped form is kept.
    """
    text = _fold_accents(unicodedata.normalize("NFKC", name)).lower()
    text = text.replace("&", " and ")
    text = _DOTTED_ABBREV.sub("", text)

    def clean(value: str) -> str:
        value = re.sub(r"[^\w\s]", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    stripped = _CJK_SUFFIXES.sub(" ", _LEGAL_SUFFIXES.sub(" ", text))
    stripped = _LEADING_ARTICLE.sub("", clean(stripped))
    return stripped or clean(text)


@dataclass(frozen=True)
class Entity:
    wikidata_id: str
    canonical_name: str
    parent_company: str | None
    headquarters: str | None
    country: str | None = None  # ISO 3166-1 alpha-2, e.g. "US"


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
                country=rec.get("country"),
            )
            for alias in rec["aliases"]:
                self._choices.append(normalize(alias))
                self._owners.append(entity)
        # Candidate positions per region, built once so a request only scores
        # the aliases inside the region it asked for.
        self._region_rows: dict[str, list[int]] = {
            region: [
                i
                for i, owner in enumerate(self._owners)
                if countries is None or owner.country in countries
            ]
            for region, countries in REGIONS.items()
        }

    def resolve(self, name: str, region: str = "world") -> Match | None:
        """Return the best match above the threshold, or None (blocking call).

        ``region`` limits the candidates to companies in that region (a key of
        ``REGIONS``); ``"world"`` searches the whole index.
        """
        if self._delay:
            time.sleep(self._delay)  # stand-in for a slow model; see discussion.md
        rows = self._region_rows[region]
        best = process.extractOne(
            normalize(name),
            {i: self._choices[i] for i in rows},
            scorer=fuzz.WRatio,
        )
        if best is None:
            return None
        _, score, idx = best
        if score / 100 < self._threshold:
            return None
        return Match(entity=self._owners[idx], score=round(score / 100, 3))
