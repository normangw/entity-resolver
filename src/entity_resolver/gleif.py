"""GLEIF (Global LEI Foundation), used to settle low-confidence matches.

GLEIF publishes the legal name, registered address and status of every company that
holds a Legal Entity Identifier (LEI), free and without a key. Only the legal name is
used: the address is often a registered agent's (Walmart's is in Wilmington, not
Bentonville), so headquarters stay as Wikidata has them. Its name search is not
usable for messy input (the legal-name filter is not an exact match and fuzzy
completion ranks near-misses first), but lookup by LEI is exact. So the tiebreak works
on candidates the local index already proposed: for each candidate that carries an
LEI, fetch its official record, then keep the candidate whose legal name best matches
the query. The LEI of each company comes from Wikidata (property P1278) when the
index is built.
"""

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from rapidfuzz import fuzz

from entity_resolver.resolver import Match, normalize

logger = logging.getLogger(__name__)

BASE_URL = "https://api.gleif.org/api/v1"
USER_AGENT = "entity-resolver-course-project/0.1 (DSAN 6700, Georgetown)"


@dataclass(frozen=True)
class GleifCompany:
    lei: str
    name: str
    status: str | None


def parse_response(payload: dict[str, Any]) -> dict[str, GleifCompany]:
    """Turn a ``lei-records`` JSON:API document into companies keyed by LEI."""
    found: dict[str, GleifCompany] = {}
    for record in payload.get("data") or []:
        entity = (record.get("attributes") or {}).get("entity") or {}
        name = (entity.get("legalName") or {}).get("name")
        if not name:
            continue
        found[record["id"]] = GleifCompany(
            lei=record["id"],
            name=name,
            status=entity.get("status"),
        )
    return found


class GleifClient(Protocol):
    async def lookup(self, leis: Sequence[str]) -> dict[str, GleifCompany]: ...


class HttpGleifClient:
    """Fetches records from the public GLEIF API, one request per lookup."""

    def __init__(self, timeout: float = 5.0, base_url: str = BASE_URL) -> None:
        self._timeout = timeout
        self._base_url = base_url

    async def lookup(self, leis: Sequence[str]) -> dict[str, GleifCompany]:
        # urllib blocks, so run it in a worker thread; its own timeout bounds it.
        return await asyncio.to_thread(self._fetch, list(leis))

    def _fetch(self, leis: list[str]) -> dict[str, GleifCompany]:
        query = urllib.parse.urlencode(
            {"filter[lei]": ",".join(leis), "page[size]": len(leis)}
        )
        request = urllib.request.Request(
            f"{self._base_url}/lei-records?{query}",
            headers={
                "Accept": "application/vnd.api+json",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return parse_response(json.load(response))


class Budget:
    """Daily cap on GLEIF requests. GLEIF is free but shared, so stay polite."""

    def __init__(self, daily_limit: int) -> None:
        self._limit = daily_limit
        self._day = date.today()
        self._used = 0

    def try_spend(self, requests: int) -> bool:
        today = date.today()
        if today != self._day:
            self._day, self._used = today, 0
        if self._used + requests > self._limit:
            return False
        self._used += requests
        return True


@dataclass(frozen=True)
class Verified:
    match: Match
    company: GleifCompany


async def tiebreak(
    query: str,
    candidates: Sequence[Match],
    client: GleifClient,
    threshold: float,
    budget: Budget | None = None,
) -> Verified | None:
    """Pick the candidate whose GLEIF legal name best matches ``query``.

    Returns None when no candidate has an LEI, the budget is spent, or no legal name
    scores at least ``threshold``. One request covers all candidates.
    """
    by_lei = {c.entity.identifier: c for c in candidates if c.entity.identifier}
    if not by_lei:
        return None
    if budget is not None and not budget.try_spend(1):
        logger.warning("GLEIF daily budget spent; skipping tiebreak")
        return None
    companies = await client.lookup(list(by_lei))
    wanted = normalize(query)
    best: Verified | None = None
    best_score = 0.0
    for lei, company in companies.items():
        if lei not in by_lei:
            continue
        score = fuzz.WRatio(wanted, normalize(company.name)) / 100
        if score > best_score:
            best_score = score
            best = Verified(Match(by_lei[lei].entity, round(score, 3)), company)
    return best if best is not None and best_score >= threshold else None
