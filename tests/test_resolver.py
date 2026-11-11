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
    """WRatio accepts a name containing a short alias; the default ratio does not."""
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
                    "aliases": ["CyberAgent", "CA"],
                }
            ]
        ),
        encoding="utf-8",
    )
    assert Resolver(path, scorer=fuzz.WRatio).resolve("Cargill Foods") is not None
    assert Resolver(path).resolve("Cargill Foods") is None  # the default is ratio


def _index(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "idx.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def _rec(qid: str, name: str, aliases: list[str], parent: str | None = None) -> dict:
    return {
        "wikidata_id": qid,
        "canonical_name": name,
        "parent_company": parent,
        "headquarters": None,
        "country": "US",
        "aliases": aliases,
    }


def test_short_names_must_match_exactly(tmp_path):
    resolver = Resolver(_index(tmp_path, [_rec("Q1", "Plexus", ["Plexus"])]))
    assert resolver.resolve("Lexus") is None  # 0.91 similar, a different company
    assert resolver.resolve("Plexus") is not None


def test_generic_words_do_not_resolve(tmp_path):
    resolver = Resolver(_index(tmp_path, [_rec("Q1", "Bank", ["Bank"])]))
    assert resolver.resolve("Bank") is None
    assert resolver.resolve("Inc.") is None


def test_same_name_prefers_top_level_company_and_flags_ambiguity(tmp_path):
    resolver = Resolver(
        _index(
            tmp_path,
            [
                _rec("Q1", "Siemens Limited", ["Siemens"], parent="Siemens"),
                _rec("Q2", "Siemens", ["Siemens AG"]),
            ],
        )
    )
    match = resolver.resolve("Siemens AG")
    assert match is not None
    assert match.entity.canonical_name == "Siemens"
    assert match.ambiguous


def test_unique_match_is_not_ambiguous(tmp_path):
    resolver = Resolver(_index(tmp_path, [_rec("Q1", "Walmart", ["Walmart"])]))
    match = resolver.resolve("Walmart")
    assert match is not None and not match.ambiguous


def make_index(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "index.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def rec(qid: str, name: str, aliases: list[str], parent: str | None = None) -> dict:
    return {
        "wikidata_id": qid,
        "canonical_name": name,
        "parent_company": parent,
        "headquarters": None,
        "country": "US",
        "aliases": aliases,
    }


def test_symbol_only_and_placeholder_input_is_unresolved(tmp_path):
    # Bitcoin's alias is a bare symbol that normalizes to nothing (#75).
    resolver = Resolver(
        make_index(
            tmp_path,
            [
                rec("Q1", "Bitcoin", ["₿", "?"]),
                rec("Q2", "AT&T", ["T", "AT&T"]),
                rec("Q3", "Visa", ["V", "Visa"]),
            ],
        )
    )
    for junk in ["\U0001f600", "?", ">", "-", "???", "T", "V", "X", "TBD", "N/A"]:
        assert resolver.resolve(junk) is None, junk
    assert resolver.resolve("Visa") is not None


def test_same_name_ties_go_to_the_lowest_q_number(tmp_path):
    resolver = Resolver(
        make_index(
            tmp_path,
            [
                rec("Q900", "Tesco Japan", ["Tesco Group"]),
                rec("Q487494", "Tesco", ["Tesco Group"]),
                rec("Q50", "Tesco Elder", ["Tesco Group"]),
            ],
        )
    )
    match = resolver.resolve("Tesco Group")
    assert match is not None
    assert match.entity.wikidata_id == "Q50"
    assert match.ambiguous


def test_a_top_level_company_still_beats_a_lower_q_subsidiary(tmp_path):
    resolver = Resolver(
        make_index(
            tmp_path,
            [
                rec("Q10", "Siemens Limited", ["Siemens"], parent="Siemens"),
                rec("Q99", "Siemens", ["Siemens"]),
            ],
        )
    )
    match = resolver.resolve("Siemens")
    assert match is not None
    assert match.entity.wikidata_id == "Q99"


def test_match_reports_the_alias_it_matched_on(tmp_path):
    resolver = Resolver(
        make_index(tmp_path, [rec("Q1", "Meta Platforms", ["Facebook", "Meta"])])
    )
    match = resolver.resolve("facebook inc")
    assert match is not None
    assert match.matched_alias == "Facebook"
    assert len(resolver.as_of) == 10  # an ISO date


def test_variants_strip_vendor_numbers_cities_and_dba():
    from entity_resolver.resolver import variants

    assert "Walmart" in variants("Walmart, Bentonville AR")
    assert "Microsoft Corp" in variants("Microsoft Corp - Redmond, WA")
    assert "Microsoft Corporation" in variants("Microsoft Corporation (Vendor #4471)")
    assert variants("Walmart Inc. d/b/a Sam's Club") == [
        "Walmart Inc. d/b/a Sam's Club",
        "Walmart Inc.",
        "Sam's Club",
    ]
    assert variants("Apple Inc.") == ["Apple Inc."]


def test_split_companies_carry_a_review_note():
    from entity_resolver.resolver import note_for

    assert "GE Vernova" in (note_for("General Electric") or "")
    assert note_for("Apple") is None
