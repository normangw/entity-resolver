"""S&P Capital IQ Company Intelligence, used to settle low-confidence matches.

The real API (``POST /v3/clientservice.json``, SPQL queries) looks a company up
by *identifier* (``ticker:exchange``, ISIN, CIQ ID, ...); it has no search by
name. So the tiebreak works on candidates the local index already proposed: for
each candidate that has an identifier, ask Capital IQ for its official name and
address, then keep the candidate whose official name best matches the query.

Only a mock client exists for now. It builds the same request body the real API
takes and answers in the shape of the documented ``GDSSDKResponse`` block, so a
real HTTP client can replace it without touching ``tiebreak``.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from rapidfuzz import fuzz

from entity_resolver.resolver import Match, normalize

logger = logging.getLogger(__name__)

_MNEMONICS = ("IQ_COMPANY_NAME", "IQ_COMPANY_ADDRESS")


@dataclass(frozen=True)
class CapIQCompany:
    identifier: str
    name: str
    address: str | None


def build_request(identifiers: Sequence[str]) -> dict[str, Any]:
    """Build the SPQL body: one GDSP request per identifier and data item."""
    return {
        "inputRequests": [
            {"function": "GDSP", "identifier": ident, "mnemonic": mnemonic}
            for ident in identifiers
            for mnemonic in _MNEMONICS
        ]
    }


def parse_response(blocks: Sequence[dict[str, Any]]) -> dict[str, CapIQCompany]:
    """Turn ``GDSSDKResponse`` blocks into companies, skipping blocks with errors."""
    values: dict[str, dict[str, str]] = {}
    for block in blocks:
        rows = block.get("Rows") or []
        if block.get("ErrMsg") or not rows:
            continue
        values.setdefault(block["Identifier"], {})[block["Mnemonic"]] = rows[0]["Row"][
            0
        ]
    return {
        ident: CapIQCompany(
            identifier=ident,
            name=data["IQ_COMPANY_NAME"],
            address=data.get("IQ_COMPANY_ADDRESS"),
        )
        for ident, data in values.items()
        if "IQ_COMPANY_NAME" in data
    }


class CapIQClient(Protocol):
    async def lookup(self, identifiers: Sequence[str]) -> dict[str, CapIQCompany]: ...


# Official names and addresses as Capital IQ would report them (mock data).
_MOCK_DATA: dict[str, tuple[str, str]] = {
    "WMT:NYSE": (
        "Walmart Inc.",
        "702 Southwest 8th Street, Bentonville, AR 72716, United States",
    ),
    "AAPL:NASDAQGS": (
        "Apple Inc.",
        "One Apple Park Way, Cupertino, CA 95014, United States",
    ),
    "BABA:NYSE": (
        "Alibaba Group Holding Limited",
        "969 West Wen Yi Road, Hangzhou, 311121, China",
    ),
    "GOOGL:NASDAQGS": (
        "Alphabet Inc.",
        "1600 Amphitheatre Parkway, Mountain View, CA 94043, United States",
    ),
}


class MockCapIQClient:
    """Answers from canned data in the real response shape; makes no network call."""

    async def lookup(self, identifiers: Sequence[str]) -> dict[str, CapIQCompany]:
        request = build_request(identifiers)
        blocks = [self._block(r) for r in request["inputRequests"]]
        return parse_response(blocks)

    @staticmethod
    def _block(req: dict[str, Any]) -> dict[str, Any]:
        base = {
            "Function": req["function"],
            "Identifier": req["identifier"],
            "Mnemonic": req["mnemonic"],
        }
        known = _MOCK_DATA.get(req["identifier"])
        if known is None:
            return {**base, "ErrMsg": "Data Not Available", "NumRows": 0, "Rows": []}
        value = known[0] if req["mnemonic"] == "IQ_COMPANY_NAME" else known[1]
        return {
            **base,
            "Headers": [req["mnemonic"]],
            "NumRows": 1,
            "NumCols": 1,
            "Rows": [{"Row": [value]}],
        }


class CapIQBudget:
    """Daily cap on Capital IQ requests (the trial plan allows 24,000 a day)."""

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
    company: CapIQCompany


async def tiebreak(
    query: str,
    candidates: Sequence[Match],
    client: CapIQClient,
    threshold: float,
    budget: CapIQBudget | None = None,
) -> Verified | None:
    """Pick the candidate whose Capital IQ name best matches ``query``.

    Returns None when no candidate has an identifier, the budget is spent, or no
    official name scores at least ``threshold``. One batched call covers all
    candidates.
    """
    by_id = {c.entity.identifier: c for c in candidates if c.entity.identifier}
    if not by_id:
        return None
    cost = len(build_request(list(by_id))["inputRequests"])
    if budget is not None and not budget.try_spend(cost):
        logger.warning("Capital IQ daily budget spent; skipping tiebreak")
        return None
    companies = await client.lookup(list(by_id))
    wanted = normalize(query)
    best: Verified | None = None
    best_score = 0.0
    for ident, company in companies.items():
        score = fuzz.WRatio(wanted, normalize(company.name)) / 100
        if score > best_score:
            best_score = score
            best = Verified(Match(by_id[ident].entity, round(score, 3)), company)
    return best if best is not None and best_score >= threshold else None
