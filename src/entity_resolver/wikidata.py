"""Live Wikidata lookup, used when a name is not in the local alias index.

The local index only knows a few companies. Wikidata also records private
subsidiaries (YouTube, Beats Electronics, ...), so on an index miss the service
asks Wikidata and reads the owner from the ``parent organization`` (P749) or
``owned by`` (P127) statements. ``lookup`` is blocking network I/O, so async
callers must run it off the event loop (``asyncio.to_thread``).

Answers, including "no such company", are cached in memory for a while: Wikidata
throttles quickly and a lookup costs three or more requests. Failures are never
cached, so a throttled name is retried on the next call.
"""

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from rapidfuzz import fuzz

from entity_resolver.resolver import Entity, Match, normalize

logger = logging.getLogger(__name__)

_API = "https://www.wikidata.org/w/api.php"
# Wikimedia asks API clients to identify themselves.
_USER_AGENT = "entity-resolver/0.1 (DSAN 6700 class project)"

_PARENT_PROPS = ("P749", "P127")  # parent organization, owned by
_HQ_PROP = "P159"  # headquarters location
# A search hit is treated as a company only if it has one of these, which rules
# out planets, people and songs that share a name with a company.
_COMPANY_PROPS = (*_PARENT_PROPS, _HQ_PROP, "P452")  # P452 = industry
_END_TIME = "P582"
# A parent named like the entity itself ("WhatsApp LLC" for WhatsApp) is its
# legal-entity wrapper, not a real owner; hop through it, at most this far.
_MAX_WRAPPER_HOPS = 2

Fetch = Callable[[dict[str, str]], dict[str, Any]]


def _http_fetch(timeout: float) -> Fetch:
    def fetch(params: dict[str, str]) -> dict[str, Any]:
        query = urllib.parse.urlencode({**params, "format": "json"})
        request = urllib.request.Request(
            f"{_API}?{query}", headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data: dict[str, Any] = json.load(response)
        return data

    return fetch


class WikidataUnavailable(Exception):
    """Wikidata could not be reached, throttled us, or sent an unusable reply."""


def _statement_ids(
    claims: dict[str, Any], prop: str, include_ended: bool = False
) -> list[str]:
    """Entity ids a property points to, skipping deprecated statements.

    Statements with an end time are skipped unless ``include_ended`` is set.
    """
    ids: list[str] = []
    for statement in claims.get(prop, []):
        snak = statement["mainsnak"]
        if (
            statement.get("rank") == "deprecated"
            or (not include_ended and _END_TIME in statement.get("qualifiers", {}))
            or snak.get("snaktype") != "value"
        ):
            continue
        ids.append(snak["datavalue"]["value"]["id"])
    return ids


class WikidataClient:
    """Resolves a company name through the Wikidata search and entity APIs."""

    def __init__(
        self,
        timeout: float = 10,
        threshold: float = 0.8,
        fetch: Fetch | None = None,
        cache_ttl: float = 3600,
        cache_size: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``cache_ttl`` is in seconds; 0 turns the cache off."""
        self._threshold = threshold
        self._fetch = fetch or _http_fetch(timeout)
        self._ttl = cache_ttl
        self._max_entries = cache_size
        self._clock = clock
        self._cache: OrderedDict[tuple[str, str], tuple[float, Match | None]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()  # lookups run on several worker threads

    def lookup(self, name: str, language: str = "en") -> Match | None:
        """Return the best company match for ``name``, or None (blocking call).

        None means Wikidata answered and had no company by that name. If it
        could not answer (network failure, HTTP 429, bad JSON) this raises
        ``WikidataUnavailable`` so an outage is not mistaken for "not found".
        """
        key = (normalize(name), language)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and self._clock() - cached[0] < self._ttl:
                self._cache.move_to_end(key)
                return cached[1]
        try:
            match = self._lookup(name, language)
        except (OSError, ValueError) as exc:  # network failure or bad JSON
            logger.warning("Wikidata lookup failed for %r: %s", name, exc)
            raise WikidataUnavailable(str(exc)) from exc
        if self._ttl > 0:
            with self._lock:
                self._cache[key] = (self._clock(), match)
                self._cache.move_to_end(key)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)
        return match

    def _lookup(self, name: str, language: str) -> Match | None:
        hits = self._fetch(
            {
                "action": "wbsearchentities",
                "search": name,
                "language": language,
                "uselang": language,
                "type": "item",
                "limit": "3",
            }
        )["search"]
        wanted = normalize(name)
        scored: list[tuple[dict[str, Any], float]] = []
        for hit in hits:
            matched = hit.get("match", {}).get("text") or hit.get("label", "")
            score = fuzz.WRatio(wanted, normalize(matched)) / 100
            if score >= self._threshold:
                scored.append((hit, score))
        if not scored:
            return None

        entities = self._fetch(
            {
                "action": "wbgetentities",
                "ids": "|".join(hit["id"] for hit, _ in scored),
                "props": "claims",
            }
        )["entities"]
        for hit, score in scored:
            claims = entities[hit["id"]].get("claims", {})
            if any(prop in claims for prop in _COMPANY_PROPS):
                return self._build(hit, claims, score, language)
        return None

    def _build(
        self, hit: dict[str, Any], claims: dict[str, Any], score: float, language: str
    ) -> Match:
        name = hit.get("label", hit["id"])
        parent_ids = [i for prop in _PARENT_PROPS for i in _statement_ids(claims, prop)]
        hq_ids = _statement_ids(claims, _HQ_PROP)[:1]
        labels = self._labels([*parent_ids, *hq_ids], language)
        # The first owner with a label wins: P749 before P127, and an owner
        # that has no label in this language (or English) is skipped.
        parent_id = next((i for i in parent_ids if i in labels), None)
        for _ in range(_MAX_WRAPPER_HOPS):
            if parent_id is None or normalize(labels[parent_id]) != normalize(name):
                break
            parent_id = self._owner_of(parent_id, labels, language)
        if parent_id is not None and normalize(labels[parent_id]) == normalize(name):
            parent_id = None  # only ever found the entity's own wrapper
        entity = Entity(
            wikidata_id=hit["id"],
            canonical_name=name,
            parent_company=labels[parent_id] if parent_id else None,
            headquarters=labels.get(hq_ids[0]) if hq_ids else None,
        )
        return Match(entity=entity, score=round(score, 3))

    def _owner_of(self, qid: str, labels: dict[str, str], language: str) -> str | None:
        """The labelled owner of ``qid``, preferring current over ended statements.

        Adds the owners' labels to ``labels``. Ended statements are a last
        resort: a wrapper entity is often merged away while the brand keeps its
        owner (WhatsApp LLC's link to Meta has an end date).
        """
        claims = self._fetch(
            {"action": "wbgetentities", "ids": qid, "props": "claims"}
        )["entities"][qid].get("claims", {})
        for include_ended in (False, True):
            ids = [
                i
                for prop in _PARENT_PROPS
                for i in _statement_ids(claims, prop, include_ended)
            ]
            labels.update(self._labels(ids, language))
            owner = next((i for i in ids if i in labels), None)
            if owner is not None:
                return owner
        return None

    def _labels(self, ids: list[str], language: str) -> dict[str, str]:
        if not ids:
            return {}
        entities = self._fetch(
            {
                "action": "wbgetentities",
                "ids": "|".join(dict.fromkeys(ids)),
                "props": "labels",
                "languages": "|".join(dict.fromkeys([language, "en"])),
            }
        )["entities"]
        out: dict[str, str] = {}
        for qid, entity in entities.items():
            labels = entity.get("labels", {})
            label = labels.get(language) or labels.get("en")
            if label:
                out[qid] = label["value"]
        return out
