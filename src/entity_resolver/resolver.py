"""The entity-resolution model: a RapidFuzz scorer over a local alias index.

The index is loaded once at startup (see ``lifespan`` in ``main.py``) and reused
for every request. ``resolve`` is synchronous and CPU-bound, so async callers
must run it off the event loop (``asyncio.to_thread``) or from a plain ``def``.
"""

import datetime as dt
import json
import re
import time
import unicodedata
from collections.abc import Callable
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

# Names this short must match exactly: one changed letter is enough to clear the
# threshold ("Lexus" vs "Plexus" scores 0.91) and the result is a different company.
_MIN_FUZZY_LENGTH = 8
# Inputs that are words or legal forms, not company names. They would otherwise
# match the first company whose name contains them ("Bank" -> "Bank Aladin Syariah").
_GENERIC_NAMES = frozenset(
    "bank banks holding holdings inc corp corporation company co ltd limited llc "
    "plc group groups stores store the and usa us uk paris london tokyo "
    "tbd tba tbc na n a nil null none unknown test misc other various".split()
)
# A real company name has at least this many letters or digits. One character is
# a placeholder ("T", "V", "-"), and a bare ticker that short is too ambiguous.
_MIN_QUERY_LENGTH = 2

# Names whose answer needs a human, with the reason. The match is still returned,
# but flagged for review with this note: the old name no longer points at one company.
_NOTES = {
    "general electric": (
        "General Electric split into GE Aerospace (the renamed parent), GE Vernova "
        "(energy) and GE HealthCare in 2023-24. Check which unit the record means."
    ),
    "hewlett packard": (
        "Hewlett-Packard split in 2015 into HP Inc. (PCs, printers) and Hewlett "
        "Packard Enterprise (servers, services). Check which one the record means."
    ),
    "time warner": (
        "Time Warner Inc. (media) became WarnerMedia and is now part of Warner Bros. "
        "Discovery. Time Warner Cable is a separate company, now part of Charter."
    ),
}
# Noise found in real vendor columns: notes in brackets, vendor numbers, a city.
_NOISE_PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NOISE_ID = re.compile(
    r"[,;\s]*(?:vendor|vend|supplier|acct|account|id)?\s*#\s*\w+", re.IGNORECASE
)
_TRAILING_PLACE = re.compile(
    r"\s*[,\-\u2013|]\s*[A-Za-z][A-Za-z .']+(?:,\s*)?[A-Z]{2}\s*$"
)
_DBA = re.compile(r"\s+(?:d\s*/\s*b\s*/\s*a|dba|doing business as)\s+", re.I)

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


def is_generic(name: str) -> bool:
    """True for input that is not a company name: a bare word or legal form
    ("Bank"), a placeholder ("TBD"), or fewer than two letters or digits ("T", "?").
    """
    query = normalize(name)
    return (
        sum(ch.isalnum() for ch in query) < _MIN_QUERY_LENGTH or query in _GENERIC_NAMES
    )


def variants(name: str) -> list[str]:
    """The names to try for one cell, most literal first.

    The text as typed, then without parentheses and vendor numbers, then without a
    trailing city and state, and for "X d/b/a Y" the legal entity X before the brand
    Y. Callers stop at the first variant that resolves.
    """
    out: list[str] = []

    def add(value: str) -> None:
        value = value.strip(" ,;-")
        if value and value not in out:
            out.append(value)

    add(name)
    parts = _DBA.split(name)
    for part in parts:
        no_notes = _NOISE_ID.sub("", _NOISE_PARENS.sub(" ", part))
        add(no_notes)
        add(_TRAILING_PLACE.sub("", no_notes))
    return out


def note_for(name: str) -> str | None:
    """The review note for a name that has been split or merged, if it has one."""
    return _NOTES.get(normalize(name))


@dataclass(frozen=True)
class Entity:
    wikidata_id: str
    canonical_name: str
    parent_company: str | None
    headquarters: str | None
    country: str | None = None  # ISO 3166-1 alpha-2, e.g. "US"
    identifier: str | None = None  # the company's LEI, used to verify GLEIF candidates
    label: str | None = None  # name in the requested language, if not English


@dataclass(frozen=True)
class Match:
    entity: Entity
    score: float  # 0.0 - 1.0
    # True when a different company scored the same, so the pick is arbitrary.
    ambiguous: bool = False
    matched_alias: str | None = None  # the stored name the input matched


def _q_number(wikidata_id: str) -> int:
    """The number in a Wikidata id ("Q487494" -> 487494); unparseable ids sort last."""
    digits = wikidata_id.lstrip("Q")
    return int(digits) if digits.isdigit() else 2**62


class Resolver:
    """Fuzzy-matches a company name against every known alias."""

    def __init__(
        self,
        index_path: Path,
        threshold: float = 0.9,
        delay_seconds: float = 0.0,
        scorer: Callable[..., float] = fuzz.ratio,
    ) -> None:
        records = json.loads(index_path.read_text(encoding="utf-8"))
        self._threshold = threshold
        self._scorer = scorer
        self._delay = delay_seconds
        self._choices: list[str] = []
        self._aliases: list[str] = []
        self._owners: list[Entity] = []
        # When the index was built; shown as the data's "as of" date.
        self.as_of = (
            dt.datetime.fromtimestamp(index_path.stat().st_mtime, dt.UTC)
            .date()
            .isoformat()
        )
        for rec in records:
            entity = Entity(
                wikidata_id=rec["wikidata_id"],
                canonical_name=rec["canonical_name"],
                parent_company=rec.get("parent_company"),
                headquarters=rec.get("headquarters"),
                country=rec.get("country"),
                identifier=rec.get("identifier"),
            )
            for alias in rec["aliases"]:
                normalized = normalize(alias)
                if not normalized:  # a symbol-only alias would match any symbol
                    continue
                self._choices.append(normalized)
                self._aliases.append(alias)
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
        query = normalize(name)
        if not query or is_generic(name):
            return None
        rows = self._region_rows[region]
        found = process.extract(
            query,
            {i: self._choices[i] for i in rows},
            scorer=self._scorer,
            limit=None,
            score_cutoff=self._threshold * 100,
        )
        if not found:
            return None
        top = found[0][1]
        # A short name only counts when it matches exactly.
        if len(query) < _MIN_FUZZY_LENGTH and top < 100:
            return None
        tied = {
            self._owners[idx].wikidata_id: idx
            for _, score, idx in found
            if score == top
        }
        # Same name, several companies: prefer a top-level company over a subsidiary
        # ("Siemens AG" -> Siemens, not Siemens Limited), then the lowest Wikidata
        # Q-number, since older and more notable entities were created first.
        ranked = sorted(
            tied.values(),
            key=lambda i: (
                self._owners[i].parent_company is not None,
                _q_number(self._owners[i].wikidata_id),
            ),
        )
        return Match(
            entity=self._owners[ranked[0]],
            score=round(top / 100, 3),
            ambiguous=len(tied) > 1,
            matched_alias=self._aliases[ranked[0]],
        )

    def candidates(
        self, name: str, region: str = "world", min_score: float = 0.0, limit: int = 3
    ) -> list[Match]:
        """Closest distinct companies scoring at least ``min_score``, best first."""
        rows = self._region_rows[region]
        found = process.extract(
            normalize(name),
            {i: self._choices[i] for i in rows},
            scorer=self._scorer,
            limit=None,
            score_cutoff=min_score * 100,
        )
        out: list[Match] = []
        seen: set[str] = set()
        for _, score, idx in found:
            entity = self._owners[idx]
            if entity.wikidata_id in seen:
                continue
            seen.add(entity.wikidata_id)
            out.append(Match(entity=entity, score=round(score / 100, 3)))
            if len(out) == limit:
                break
        return out
