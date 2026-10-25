import asyncio
from collections.abc import Sequence

from entity_resolver.capiq import (
    CapIQBudget,
    CapIQCompany,
    MockCapIQClient,
    build_request,
    parse_response,
    tiebreak,
)
from entity_resolver.resolver import Entity, Match


def match(name: str, ident: str | None, score: float = 0.7) -> Match:
    return Match(
        entity=Entity(
            wikidata_id=f"Q-{name}",
            canonical_name=name,
            parent_company=None,
            headquarters=None,
            identifier=ident,
        ),
        score=score,
    )


def test_build_request_has_one_gdsp_query_per_identifier_and_mnemonic():
    body = build_request(["IBM:NYSE"])
    assert body == {
        "inputRequests": [
            {"function": "GDSP", "identifier": "IBM:NYSE", "mnemonic": m}
            for m in ("IQ_COMPANY_NAME", "IQ_COMPANY_ADDRESS")
        ]
    }


def test_parse_response_skips_error_blocks():
    blocks = [
        {
            "Identifier": "A:X",
            "Mnemonic": "IQ_COMPANY_NAME",
            "Rows": [{"Row": ["Acme Corp"]}],
        },
        {"Identifier": "B:X", "Mnemonic": "IQ_COMPANY_NAME", "ErrMsg": "nope"},
    ]
    assert parse_response(blocks) == {"A:X": CapIQCompany("A:X", "Acme Corp", None)}


def test_mock_client_returns_known_and_drops_unknown():
    found = asyncio.run(MockCapIQClient().lookup(["WMT:NYSE", "ZZZ:NONE"]))
    assert list(found) == ["WMT:NYSE"]
    assert found["WMT:NYSE"].name == "Walmart Inc."


def test_tiebreak_picks_candidate_with_closest_official_name():
    candidates = [match("Apple", "AAPL:NASDAQGS"), match("Walmart", "WMT:NYSE")]
    result = asyncio.run(
        tiebreak("Wal Mart", candidates, MockCapIQClient(), threshold=0.8)
    )
    assert result is not None
    assert result.match.entity.canonical_name == "Walmart"


def test_tiebreak_returns_none_when_no_name_is_close_enough():
    candidates = [match("Walmart", "WMT:NYSE")]
    result = asyncio.run(
        tiebreak("Totally Different", candidates, MockCapIQClient(), threshold=0.8)
    )
    assert result is None


def test_tiebreak_skips_candidates_without_identifier():
    class Fail:
        async def lookup(self, identifiers: Sequence[str]):
            raise AssertionError("must not be called")

    assert asyncio.run(tiebreak("x", [match("Nolisted", None)], Fail(), 0.8)) is None


def test_budget_blocks_tiebreak_when_spent():
    budget = CapIQBudget(daily_limit=1)  # one tiebreak costs two requests
    result = asyncio.run(
        tiebreak(
            "Wal Mart",
            [match("Walmart", "WMT:NYSE")],
            MockCapIQClient(),
            0.8,
            budget,
        )
    )
    assert result is None


def test_budget_allows_up_to_the_limit():
    budget = CapIQBudget(daily_limit=4)
    assert budget.try_spend(2)
    assert budget.try_spend(2)
    assert not budget.try_spend(1)
