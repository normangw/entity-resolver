import os
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_main import wait_ready

from entity_resolver.config import settings
from entity_resolver.main import app
from entity_resolver.wikidata import WikidataClient, WikidataUnavailable


def statement(qid: str, **extra: Any) -> dict[str, Any]:
    """A minimal item-valued Wikidata statement."""
    return {
        "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": qid}}},
        "rank": "normal",
        **extra,
    }


def fake_wikidata(
    hits: list[dict[str, Any]],
    claims: dict[str, dict[str, Any]],
    labels: dict[str, dict[str, str]],
):
    """A ``fetch`` replacement serving canned search, claims and label calls."""

    def fetch(params: dict[str, str]) -> dict[str, Any]:
        if params["action"] == "wbsearchentities":
            return {"search": hits}
        if params["action"] == "wbgetclaims":
            return {"claims": {"P31": claims.get(params["entity"], {}).get("P31", [])}}
        ids = params["ids"].split("|")
        if params["props"] == "claims":
            return {"entities": {i: {"claims": claims.get(i, {})} for i in ids}}
        return {
            "entities": {
                i: {"labels": {"en": {"value": v}} if (v := labels.get(i)) else {}}
                for i in ids
            }
        }

    return fetch


def hit(qid: str, label: str) -> dict[str, Any]:
    return {"id": qid, "label": label, "match": {"type": "label", "text": label}}


# Shaped like the real data: YouTube has no P749, only P127 (owned by), with one
# ended owner and Google as the single current one; Beats has P749 pointing at
# Apple.
YOUTUBE = fake_wikidata(
    [hit("Q866", "YouTube")],
    {
        "Q866": {
            "P127": [
                statement("Q112247183", qualifiers={"P582": [{}]}),
                statement("Q95", qualifiers={"P580": [{}]}),
            ]
        }
    },
    {"Q95": "Google"},
)
BEATS = fake_wikidata(
    [hit("Q1961036", "Beats Electronics")],
    {"Q1961036": {"P749": [statement("Q312")], "P127": [statement("Q312")]}},
    {"Q312": "Apple Inc."},
)


def test_youtube_resolves_to_google():
    match = WikidataClient(fetch=YOUTUBE).lookup("YouTube")
    assert match is not None
    assert match.entity.canonical_name == "YouTube"
    assert match.entity.wikidata_id == "Q866"
    assert match.entity.parent_company == "Google"


def test_beats_electronics_resolves_to_apple():
    match = WikidataClient(fetch=BEATS).lookup("Beats Electronics")
    assert match is not None
    assert match.entity.parent_company == "Apple Inc."


def test_hit_without_company_properties_is_skipped():
    fetch = fake_wikidata([hit("Q308", "Mercury")], {"Q308": {"P31": []}}, {})
    assert WikidataClient(fetch=fetch).lookup("Mercury") is None


def test_ended_and_deprecated_owners_are_ignored():
    claims = {
        "Q1": {
            "P749": [
                statement("Q10", qualifiers={"P582": [{}]}),
                statement("Q11", rank="deprecated"),
                statement("Q12"),
            ]
        }
    }
    fetch = fake_wikidata(
        [hit("Q1", "Acme")], claims, {"Q10": "Old", "Q11": "Bad", "Q12": "Current"}
    )
    match = WikidataClient(fetch=fetch).lookup("Acme")
    assert match is not None
    assert match.entity.parent_company == "Current"


def test_search_hit_below_threshold_is_unresolved():
    fetch = fake_wikidata(
        [hit("Q1", "Zyxwv Holdings")], {"Q1": {"P749": [statement("Q2")]}}, {}
    )
    assert WikidataClient(fetch=fetch).lookup("YouTube") is None


def stake(fraction: float) -> dict[str, Any]:
    """A proportion (P1107) qualifier, as Wikidata stores it."""
    return {"P1107": [{"datavalue": {"value": {"amount": f"+{fraction}"}}}]}


def parent_of(name: str, claims: dict[str, dict[str, Any]], labels: dict[str, str]):
    fetch = fake_wikidata([hit("Q1", name)], {"Q1": {"P159": []}, **claims}, labels)
    match = WikidataClient(fetch=fetch).lookup(name)
    assert match is not None
    return match.entity.parent_company


def test_minority_shareholders_are_not_parents():
    # Shaped like Caterpillar: only 6-9% holders, all of them investment firms.
    owners = [
        statement("Q10", qualifiers=stake(0.0867)),
        statement("Q11", qualifiers=stake(0.081)),
    ]
    labels = {"Q10": "The Vanguard Group", "Q11": "State Street"}
    assert parent_of("Caterpillar", {"Q1": {"P127": owners}}, labels) is None


def test_several_unqualified_owners_are_ambiguous():
    # Shaped like Foxconn: two owners and nothing to say which one controls it.
    owners = [statement("Q10"), statement("Q11")]
    labels = {"Q10": "Some Founder", "Q11": "The Vanguard Group"}
    assert parent_of("Foxconn", {"Q1": {"P127": owners}}, labels) is None


def test_a_person_is_never_the_parent():
    human = {"P31": [statement("Q5")]}
    claims = {"Q1": {"P127": [statement("Q10")]}, "Q10": human}
    assert parent_of("Spanx", claims, {"Q10": "Sara Blakely"}) is None


def test_majority_owner_is_the_parent():
    claims = {"Q1": {"P127": [statement("Q10", qualifiers=stake(0.72))]}}
    assert parent_of("Acme Works", claims, {"Q10": "Acme Holdings"}) == "Acme Holdings"


def test_parent_organization_beats_owned_by():
    claims = {"Q1": {"P749": [statement("Q10")], "P127": [statement("Q11")]}}
    labels = {"Q10": "Real Parent", "Q11": "Other Owner"}
    assert parent_of("Acme Works", claims, labels) == "Real Parent"


def test_error_body_with_http_200_raises_unavailable(monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return b'{"error": {"code": "ratelimited", "info": "slow down"}}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Reply())
    with pytest.raises(WikidataUnavailable, match="ratelimited"):
        WikidataClient().lookup("YouTube")


def test_parent_that_is_the_entitys_own_wrapper_is_skipped():
    # Shaped like WhatsApp: owned by "WhatsApp LLC", whose link to Meta has an
    # end date, so it is only used because the wrapper has nothing current.
    fetch = fake_wikidata(
        [hit("Q1", "WhatsApp")],
        {
            "Q1": {"P127": [statement("Q2")]},
            "Q2": {"P749": [statement("Q3", qualifiers={"P582": [{}]})]},
        },
        {"Q2": "WhatsApp LLC", "Q3": "Meta Platforms"},
    )
    match = WikidataClient(fetch=fetch).lookup("WhatsApp")
    assert match is not None
    assert match.entity.parent_company == "Meta Platforms"


def test_wrapper_with_no_owner_leaves_parent_unknown():
    fetch = fake_wikidata(
        [hit("Q1", "WhatsApp")],
        {"Q1": {"P127": [statement("Q2")]}},
        {"Q2": "WhatsApp LLC"},
    )
    match = WikidataClient(fetch=fetch).lookup("WhatsApp")
    assert match is not None
    assert match.entity.parent_company is None


def test_company_with_no_owner_statement_resolves_without_parent():
    # Shaped like Instagram: Wikidata has a headquarters but no owner at all.
    fetch = fake_wikidata(
        [hit("Q1", "Instagram")],
        {"Q1": {"P159": [statement("Q2")]}},
        {"Q2": "Menlo Park"},
    )
    match = WikidataClient(fetch=fetch).lookup("Instagram")
    assert match is not None
    assert match.entity.parent_company is None
    assert match.entity.headquarters == "Menlo Park"


def test_network_failure_raises_unavailable():
    def down(params: dict[str, str]) -> dict[str, Any]:
        raise TimeoutError("timed out")

    with pytest.raises(WikidataUnavailable):
        WikidataClient(fetch=down).lookup("YouTube")


def test_http_429_raises_unavailable():
    def throttled(params: dict[str, str]) -> dict[str, Any]:
        raise urllib.error.HTTPError(
            "https://www.wikidata.org", 429, "Too Many Requests", Message(), None
        )

    with pytest.raises(WikidataUnavailable):
        WikidataClient(fetch=throttled).lookup("YouTube")


def test_resolve_endpoint_falls_back_to_wikidata(monkeypatch):
    monkeypatch.setattr(settings, "wikidata_fallback", True)
    monkeypatch.setattr(
        "entity_resolver.main.WikidataClient",
        lambda timeout, threshold, cache_ttl: WikidataClient(
            timeout, threshold, fetch=YOUTUBE
        ),
    )
    with TestClient(app) as client:
        wait_ready(client)
        data = client.post("/resolve", json={"company_name": "YouTube"}).json()
    assert data["status"] == "resolved"
    assert data["parent_company"] == "Google"
    assert data["source"] == "wikidata"


def test_resolve_endpoint_does_not_call_wikidata_by_default():
    with TestClient(app) as client:
        wait_ready(client)
        data = client.post("/resolve", json={"company_name": "YouTube"}).json()
    assert data["status"] == "unresolved"
    assert data["source"] is None


class Counting:
    """Wraps a fetch and counts how many Wikidata requests it served."""

    def __init__(self, fetch):
        self._fetch = fetch
        self.calls = 0

    def __call__(self, params: dict[str, str]) -> dict[str, Any]:
        self.calls += 1
        return self._fetch(params)


def test_repeat_lookup_is_served_from_cache():
    fetch = Counting(YOUTUBE)
    client = WikidataClient(fetch=fetch)
    first = client.lookup("YouTube")
    served = fetch.calls
    assert served > 0
    assert client.lookup("youtube") == first  # same normalized name
    assert fetch.calls == served


def test_not_found_is_cached_too():
    fetch = Counting(fake_wikidata([], {}, {}))
    client = WikidataClient(fetch=fetch)
    assert client.lookup("Zyxwv Quantum Bakery") is None
    assert client.lookup("Zyxwv Quantum Bakery") is None
    assert fetch.calls == 1


def test_failures_are_not_cached():
    attempts = 0

    def flaky(params: dict[str, str]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("timed out")
        return YOUTUBE(params)

    client = WikidataClient(fetch=flaky)
    with pytest.raises(WikidataUnavailable):
        client.lookup("YouTube")
    match = client.lookup("YouTube")
    assert match is not None
    assert match.entity.parent_company == "Google"


def test_cache_entry_expires_after_ttl():
    now = 1000.0
    fetch = Counting(YOUTUBE)
    client = WikidataClient(fetch=fetch, cache_ttl=60, clock=lambda: now)
    client.lookup("YouTube")
    served = fetch.calls
    now += 59
    client.lookup("YouTube")
    assert fetch.calls == served
    now += 2
    client.lookup("YouTube")
    assert fetch.calls > served


def test_cache_evicts_the_least_recently_used_name():
    fetch = Counting(fake_wikidata([], {}, {}))
    client = WikidataClient(fetch=fetch, cache_size=2)
    for name in ("Alpha", "Beta"):
        client.lookup(name)
    client.lookup("Alpha")  # Beta is now the least recently used
    client.lookup("Gamma")  # evicts Beta
    before = fetch.calls
    client.lookup("Alpha")
    assert fetch.calls == before
    client.lookup("Beta")
    assert fetch.calls == before + 1


def test_zero_ttl_disables_the_cache():
    fetch = Counting(fake_wikidata([], {}, {}))
    client = WikidataClient(fetch=fetch, cache_ttl=0)
    client.lookup("Alpha")
    client.lookup("Alpha")
    assert fetch.calls == 2


@pytest.mark.skipif(not os.environ.get("RUN_LIVE_TESTS"), reason="hits real Wikidata")
@pytest.mark.parametrize(
    ("name", "parent"),
    [("YouTube", "Google"), ("Beats Electronics", "Apple Inc.")],
)
def test_live_wikidata_subsidiary_parents(name: str, parent: str):
    match = WikidataClient().lookup(name)
    assert match is not None
    assert match.entity.parent_company == parent


def test_resolve_endpoint_returns_503_when_wikidata_is_down(monkeypatch):
    def down(params: dict[str, str]) -> dict[str, Any]:
        raise TimeoutError("timed out")

    monkeypatch.setattr(settings, "wikidata_fallback", True)
    monkeypatch.setattr(
        "entity_resolver.main.WikidataClient",
        lambda timeout, threshold, cache_ttl: WikidataClient(
            timeout, threshold, fetch=down
        ),
    )
    with TestClient(app) as client:
        wait_ready(client)
        r = client.post("/resolve", json={"company_name": "YouTube"})
    assert r.status_code == 503


def test_resolve_endpoint_skips_wikidata_for_a_region_filter(monkeypatch):
    # Wikidata hits have no country, so a region request must not use them.
    monkeypatch.setattr(settings, "wikidata_fallback", True)
    monkeypatch.setattr(
        "entity_resolver.main.WikidataClient",
        lambda timeout, threshold, cache_ttl: WikidataClient(
            timeout, threshold, fetch=YOUTUBE
        ),
    )
    with TestClient(app) as client:
        wait_ready(client)
        data = client.post(
            "/resolve", json={"company_name": "YouTube", "region": "cn"}
        ).json()
    assert data["status"] == "unresolved"
    assert data["source"] is None
