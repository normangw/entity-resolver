import asyncio
import json
from collections.abc import Sequence

from entity_resolver.gleif import (
    Budget,
    GleifCompany,
    HttpGleifClient,
    parse_response,
    tiebreak,
)
from entity_resolver.resolver import Entity, Match


def match(name: str, lei: str | None, score: float = 0.7) -> Match:
    return Match(
        entity=Entity(
            wikidata_id=f"Q-{name}",
            canonical_name=name,
            parent_company=None,
            headquarters=None,
            identifier=lei,
        ),
        score=score,
    )


def record(lei: str, name: str, **entity) -> dict:
    return {
        "id": lei,
        "attributes": {"entity": {"legalName": {"name": name}, **entity}},
    }


class FakeClient:
    """Answers from a dict of LEI -> legal name; records what it was asked."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names
        self.calls: list[list[str]] = []

    async def lookup(self, leis: Sequence[str]) -> dict[str, GleifCompany]:
        self.calls.append(list(leis))
        return {
            lei: GleifCompany(lei, self.names[lei], "ACTIVE")
            for lei in leis
            if lei in self.names
        }


def test_parse_response_reads_name_and_status():
    payload = {
        "data": [
            record("LEI1", "Acme Corp", status="ACTIVE"),
            {"id": "LEI2", "attributes": {"entity": {}}},  # no name: skipped
        ]
    }
    found = parse_response(payload)
    assert found == {"LEI1": GleifCompany("LEI1", "Acme Corp", "ACTIVE")}


def test_parse_response_handles_empty_document():
    assert parse_response({}) == {}


def test_http_client_builds_one_batched_request(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return json.dumps({"data": [record("A1", "Acme")]}).encode()

    def fake_urlopen(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    found = asyncio.run(HttpGleifClient(timeout=3).lookup(["A1", "B2"]))
    assert "filter%5Blei%5D=A1%2CB2" in seen["url"]
    assert seen["timeout"] == 3
    assert list(found) == ["A1"]


def test_tiebreak_picks_candidate_with_closest_legal_name():
    client = FakeClient({"L-APPLE": "Apple Inc.", "L-WMT": "WALMART INC."})
    candidates = [match("Apple", "L-APPLE"), match("Walmart", "L-WMT")]
    result = asyncio.run(tiebreak("Wal Mart", candidates, client, threshold=0.8))
    assert result is not None
    assert result.match.entity.canonical_name == "Walmart"
    assert len(client.calls) == 1  # both candidates in one request


def test_tiebreak_returns_none_when_no_name_is_close_enough():
    client = FakeClient({"L-WMT": "WALMART INC."})
    result = asyncio.run(
        tiebreak("Totally Different", [match("Walmart", "L-WMT")], client, 0.8)
    )
    assert result is None


def test_tiebreak_ignores_records_for_unrequested_leis():
    class Chatty(FakeClient):
        async def lookup(self, leis):
            return {"OTHER": GleifCompany("OTHER", "Wal Mart", "ACTIVE")}

    result = asyncio.run(
        tiebreak("Wal Mart", [match("Walmart", "L-WMT")], Chatty({}), 0.8)
    )
    assert result is None


def test_tiebreak_skips_candidates_without_lei():
    client = FakeClient({})
    assert asyncio.run(tiebreak("x", [match("Nolisted", None)], client, 0.8)) is None
    assert client.calls == []


def test_budget_blocks_tiebreak_when_spent():
    client = FakeClient({"L-WMT": "WALMART INC."})
    budget = Budget(daily_limit=1)
    candidates = [match("Walmart", "L-WMT")]
    assert asyncio.run(tiebreak("Wal Mart", candidates, client, 0.8, budget))
    assert asyncio.run(tiebreak("Wal Mart", candidates, client, 0.8, budget)) is None
    assert len(client.calls) == 1


def test_budget_allows_up_to_the_limit():
    budget = Budget(daily_limit=2)
    assert budget.try_spend(1)
    assert budget.try_spend(1)
    assert not budget.try_spend(1)
