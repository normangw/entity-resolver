from pathlib import Path

from entity_resolver.resolver import Resolver, normalize

INDEX = (
    Path(__file__).parents[1] / "src" / "entity_resolver" / "data" / "companies.json"
)


def test_normalize_strips_suffixes_and_punctuation():
    assert normalize("Wal-Mart Stores, Inc.") == "wal mart"


def test_resolver_loads_index_and_matches():
    match = Resolver(INDEX).resolve("Apple Computer")
    assert match is not None
    assert match.entity.canonical_name == "Apple Inc."
