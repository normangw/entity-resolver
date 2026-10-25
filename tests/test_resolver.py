import json
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


def test_normalize_unicode_and_accents():
    assert normalize("Nestlé S.A.") == "nestle"
    assert normalize("ＡＰＰＬＥ Inc.") == "apple"  # full-width letters


def test_normalize_symbols_articles_and_foreign_suffixes():
    assert normalize("The Coca-Cola Company") == "coca cola"
    assert normalize("Johnson & Johnson") == "johnson and johnson"
    assert normalize("Siemens AG") == "siemens"
    assert normalize("Volkswagen GmbH") == "volkswagen"


def test_normalize_cjk_suffixes():
    assert normalize("上海航空股份有限公司") == "上海航空"
    assert normalize("トヨタ株式会社") == "トヨタ"


def test_normalize_keeps_name_when_stripping_would_empty_it():
    assert normalize("Group Inc.") == "group inc"


def write_index(tmp_path: Path) -> Path:
    path = tmp_path / "index.json"
    path.write_text(
        json.dumps(
            [
                {
                    "wikidata_id": "Q1",
                    "canonical_name": "Delta Air Lines",
                    "parent_company": None,
                    "headquarters": "Atlanta, United States",
                    "country": "US",
                    "aliases": ["Delta"],
                },
                {
                    "wikidata_id": "Q2",
                    "canonical_name": "Delta Electronics",
                    "parent_company": None,
                    "headquarters": "Taipei, Taiwan",
                    "country": "TW",
                    "aliases": ["Delta"],
                },
                {
                    "wikidata_id": "Q3",
                    "canonical_name": "Siemens",
                    "parent_company": None,
                    "headquarters": "Munich, Germany",
                    "country": "DE",
                    "aliases": ["Siemens AG"],
                },
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_region_restricts_candidates(tmp_path):
    resolver = Resolver(write_index(tmp_path))
    us = resolver.resolve("Delta", region="us")
    assert us is not None
    assert us.entity.country == "US"
    assert resolver.resolve("Siemens", region="us") is None
    assert resolver.resolve("Siemens", region="world") is not None


def test_eu_region_includes_member_states(tmp_path):
    resolver = Resolver(write_index(tmp_path))
    match = resolver.resolve("Siemens", region="eu")
    assert match is not None
    assert match.entity.canonical_name == "Siemens"


def test_bundled_index_has_countries():
    match = Resolver(INDEX).resolve("Shanghai Airlines", region="cn")
    assert match is not None
    assert match.entity.country == "CN"


def test_candidates_are_distinct_companies_best_first(tmp_path):
    resolver = Resolver(write_index(tmp_path))
    found = resolver.candidates("Delta", min_score=0.5)
    assert [m.entity.wikidata_id for m in found] == ["Q1", "Q2"]  # one per company
    assert found[0].score >= found[1].score
    assert (
        resolver.candidates("Delta", region="us", min_score=0.5)[0].entity.country
        == "US"
    )


def test_scorer_is_configurable(tmp_path):
    """WRatio accepts a short unrelated name via partial matching; ratio does not."""
    from rapidfuzz import fuzz

    path = tmp_path / "index.json"
    path.write_text(
        json.dumps(
            [
                {
                    "wikidata_id": "Q9",
                    "canonical_name": "CyberAgent",
                    "parent_company": None,
                    "headquarters": None,
                    "aliases": ["CyberAgent"],
                }
            ]
        ),
        encoding="utf-8",
    )
    assert Resolver(path).resolve("Cargill") is not None  # default WRatio
    assert Resolver(path, scorer=fuzz.ratio).resolve("Cargill") is None
