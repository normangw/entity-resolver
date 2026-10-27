"""Generate the labeled evaluation set from a built index.

    uv run python scripts/make_eval_set.py --index data/companies.full.json \\
        --out eval/queries.jsonl

Writes one JSON object per line: ``query``, ``expected`` (Wikidata ID or null),
``category`` and, for ambiguous names, ``region``. Categories:

- variant: hand-written messy forms of well-known listed companies. Review these.
- typo, suffix, multilingual, ambiguous: generated from the index with a fixed
  seed, so they are reproducible but synthetic (the label is true by construction).
- not_listed: private companies and gibberish that must come back unresolved.

Every hand-written entry is checked against the index; ones that cannot be placed
are reported and left out instead of being guessed.
"""

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from entity_resolver.resolver import REGIONS, normalize

# (messy query, canonical English label on Wikidata, ISO country)
VARIANTS = [
    ("Wal-Mart Stores, Inc.", "Walmart", "US"),
    ("MICROSOFT CORPORATION", "Microsoft", "US"),
    ("Apple Computer", "Apple Inc.", "US"),
    ("Alphabet Incorporated", "Alphabet Inc.", "US"),
    ("Amazon.com, Inc.", "Amazon", "US"),
    ("Tesla Motors", "Tesla", "US"),
    ("NVIDIA Corp", "Nvidia", "US"),
    ("The Coca-Cola Co.", "The Coca-Cola Company", "US"),
    ("Pepsico Inc", "PepsiCo", "US"),
    ("The Boeing Company", "Boeing", "US"),
    ("Ford Motor Co", "Ford Motor Company", "US"),
    ("Intel Corporation", "Intel", "US"),
    ("JP Morgan Chase & Co", "JPMorgan Chase", "US"),
    ("Toyota Motor Corp.", "Toyota", "JP"),
    ("Sony Corp", "Sony Group", "JP"),
    ("Honda Motor Co., Ltd.", "Honda", "JP"),
    ("Nintendo Co Ltd", "Nintendo", "JP"),
    ("Volkswagen AG", "Volkswagen Group", "DE"),
    ("Siemens AG", "Siemens", "DE"),
    ("Deutsche Bank AG", "Deutsche Bank", "DE"),
    ("Bayerische Motoren Werke", "BMW", "DE"),
    ("SAP SE", "SAP", "DE"),
    ("Nestle S.A.", "Nestlé", "CH"),
    ("Novartis AG", "Novartis", "CH"),
    ("Samsung Electronics Co., Ltd.", "Samsung Electronics", "KR"),
    ("Tencent Holdings Ltd", "Tencent", "CN"),
    ("Alibaba Group Holding Limited", "Alibaba Group", "CN"),
    ("Royal Dutch Shell", "Shell", "GB"),
    ("HSBC Holdings plc", "HSBC", "GB"),
    ("BP p.l.c.", "BP", "GB"),
    ("Unilever PLC", "Unilever", "GB"),
    ("LVMH Moet Hennessy Louis Vuitton", "LVMH", "FR"),
    ("TotalEnergies SE", "TotalEnergies", "FR"),
    ("Airbus SE", "Airbus", "FR"),
    ("ASML Holding N.V.", "ASML Holding", "NL"),
    ("Reliance Industries Limited", "Reliance Industries Limited", "IN"),
    ("Tata Consultancy Services Ltd", "Tata Consultancy Services", "IN"),
    ("Royal Bank of Canada", "Royal Bank of Canada", "CA"),
    ("BHP Group Limited", "BHP Group", "AU"),
    ("Petrobras", "Petrobras", "BR"),
]

# Private or otherwise unlisted: nothing in a listed-company index should match.
NOT_LISTED = [
    "Cargill",
    "Koch Industries",
    "SpaceX",
    "Robert Bosch GmbH",
    "IKEA",
    "Mars, Incorporated",
    "Aldi Nord",
    "LEGO",
    "Rolex",
    "Bechtel",
    "Publix Super Markets",
    "Fidelity Investments",
    "Epic Systems",
    "Meijer",
    "Mary Kay",
    "Hallmark Cards",
    "Chick-fil-A",
    "Wegmans Food Markets",
    "S. C. Johnson & Son",
    "Medline Industries",
    "Huawei Technologies",
    "Dr. August Oetker",
    "Mercadona",
    "Decathlon",
    "Haribo",
    "asdfgh qwerty",
    "xxxxxxxx",
    "123456",
    "Lorem Ipsum Dolor",
    "The Quick Brown Fox",
    "zzyzx holdings",
    "Acme Widget Corp of Atlantis",
    "blah blah",
    "Q",
]

EU_CODES = REGIONS["eu"]


def region_of(country: str) -> str | None:
    for key in ("us", "cn", "gb", "jp"):
        if country in REGIONS[key]:
            return key
    return "eu" if country in EU_CODES else None


def typo(name: str, rng: random.Random) -> str:
    i = rng.randrange(1, len(name) - 1)
    kind = rng.choice(["delete", "swap", "double"])
    if kind == "delete":
        return name[:i] + name[i + 1 :]
    if kind == "swap":
        return name[:i] + name[i + 1] + name[i] + name[i + 2 :]
    return name[:i] + name[i] + name[i:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("eval/queries.jsonl"))
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    records = json.loads(args.index.read_text(encoding="utf-8"))
    by_label: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_alias: dict[str, set[str]] = defaultdict(set)  # normalized alias -> QIDs
    owner = {r["wikidata_id"]: r for r in records}
    for r in records:
        by_label[(r["canonical_name"].casefold(), r["country"])].append(r)
        for alias in r["aliases"]:
            by_alias[normalize(alias)].add(r["wikidata_id"])

    rows: list[dict] = []

    for query, label, country in VARIANTS:
        hits = by_label.get((label.casefold(), country), [])
        if len(hits) != 1:
            print(
                f"SKIPPED variant {query!r}: {len(hits)} index records labelled "
                f"{label!r} in {country}"
            )
            continue
        rows.append(
            {"query": query, "expected": hits[0]["wikidata_id"], "category": "variant"}
        )

    # A company is a fair target only if its normalized canonical name is unique.
    unique = [
        r
        for r in records
        if len(by_alias[normalize(r["canonical_name"])]) == 1
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9 .&'-]{6,}", r["canonical_name"])
    ]
    rng.shuffle(unique)
    for r in unique[:30]:
        rows.append(
            {
                "query": typo(r["canonical_name"], rng),
                "expected": r["wikidata_id"],
                "category": "typo",
            }
        )
    for r in unique[30:55]:
        suffix = rng.choice([" INC", " Corp.", " Ltd", " Co., Ltd.", " Holdings"])
        rows.append(
            {
                "query": r["canonical_name"].upper() + suffix,
                "expected": r["wikidata_id"],
                "category": "suffix",
            }
        )

    foreign = []
    for r in records:
        for alias in r["aliases"]:
            if not alias.isascii() and len(by_alias[normalize(alias)]) == 1:
                foreign.append((alias, r["wikidata_id"]))
                break
    rng.shuffle(foreign)
    for alias, qid in foreign[:30]:
        rows.append({"query": alias, "expected": qid, "category": "multilingual"})

    ambiguous = []
    for alias, qids in by_alias.items():
        countries = {owner[q]["country"] for q in qids}
        if len(qids) > 1 and len(countries) > 1:
            for q in qids:
                region = region_of(owner[q]["country"])
                in_region = [
                    x for x in qids if region and owner[x]["country"] in REGIONS[region]
                ]
                if region and len(in_region) == 1:
                    ambiguous.append((alias, q, region))
    rng.shuffle(ambiguous)
    for alias, qid, region in ambiguous[:20]:
        rows.append(
            {"query": alias, "expected": qid, "category": "ambiguous", "region": region}
        )

    for name in NOT_LISTED:
        if by_alias.get(normalize(name)):
            print(f"DROPPED not_listed {name!r}: exactly matches an index alias")
            continue
        rows.append({"query": name, "expected": None, "category": "not_listed"})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    counts = defaultdict(int)
    for r in rows:
        counts[r["category"]] += 1
    print(f"wrote {len(rows)} queries to {args.out}: {dict(counts)}")


if __name__ == "__main__":
    main()
