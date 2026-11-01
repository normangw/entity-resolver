"""Build the alias index from Wikidata (offline; run it, then point the service at it).

Pulls every company that is listed on a stock exchange (statement P414 with no end
date), country by country so each SPARQL query stays under the public endpoint's
60 s limit, then fetches multilingual labels and aliases in batches, and each
company's Legal Entity Identifier (P1278), stored as ``identifier``, which the GLEIF
tiebreak uses. Writes the
index in the same shape as ``src/entity_resolver/data/companies.json`` plus a
``.meta.json`` sidecar with the build date and counts.

    uv run python scripts/build_index.py --out data/companies.full.json

Standard library only. Per-country results are cached in ``--cache-dir`` so an
interrupted run resumes instead of starting over.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "entity-resolver-course-project/0.1 (DSAN 6700, Georgetown)"
PAUSE_SECONDS = 1.0  # be polite to the public endpoint
ALIAS_BATCH = 250

# Languages whose labels and aliases go into the index.
LANGUAGES = (
    "en mul zh zh-hans zh-hant zh-cn zh-hk zh-tw ja ko de fr es it pt ru ar hi tr nl sv"
).split()


def sparql(query: str, attempts: int = 4) -> list[dict[str, dict[str, str]]]:
    """Run a query and return its bindings, retrying with backoff on failures."""
    data = urllib.parse.urlencode({"query": query}).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)["results"]["bindings"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            if attempt == attempts:
                raise
            wait = 5 * attempt
            print(f"  retry {attempt} after {err!r}; waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def val(row: dict[str, dict[str, str]], key: str) -> str | None:
    return row[key]["value"] if key in row else None


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def fetch_countries() -> list[tuple[str, str, str]]:
    """Every item with an ISO 3166-1 alpha-2 code: (QID, ISO code, English name)."""
    rows = sparql(
        """
        SELECT ?c ?iso ?cLabel WHERE {
          ?c wdt:P297 ?iso .
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
        }
        """
    )
    return sorted(
        {(qid(r["c"]["value"]), r["iso"]["value"], r["cLabel"]["value"]) for r in rows},
        key=lambda t: t[1],
    )


def fetch_listed(country_qid: str) -> list[dict[str, dict[str, str]]]:
    """Listed companies in one country, one row per (company, listing, HQ, parent)."""
    return sparql(
        f"""
        SELECT ?c ?cLabel ?hqLabel ?hqCountry ?parentLabel WHERE {{
          ?c wdt:P17 wd:{country_qid} .
          ?c p:P414 ?st . ?st ps:P414 ?ex .
          FILTER NOT EXISTS {{ ?st pq:P582 ?end }}
          OPTIONAL {{ ?c wdt:P159 ?hq . OPTIONAL {{ ?hq wdt:P17 ?hqCountry }} }}
          OPTIONAL {{ ?c wdt:P749 ?parent }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}
        }}
        """
    )


def fetch_aliases(qids: list[str]) -> dict[str, list[str]]:
    """Labels and alternative labels in ``LANGUAGES`` for a batch of companies."""
    values = " ".join(f"wd:{q}" for q in qids)
    langs = ", ".join(f'"{lang}"' for lang in LANGUAGES)
    rows = sparql(
        f"""
        SELECT ?c ?alias WHERE {{
          VALUES ?c {{ {values} }}
          {{ ?c rdfs:label ?alias }} UNION {{ ?c skos:altLabel ?alias }}
          FILTER(LANG(?alias) IN ({langs}))
        }}
        """
    )
    found: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        found[qid(r["c"]["value"])].append(r["alias"]["value"])
    return found


def fetch_leis(qids: list[str]) -> dict[str, str]:
    """Legal Entity Identifier (Wikidata P1278) for a batch of companies, if any."""
    values = " ".join(f"wd:{q}" for q in qids)
    rows = sparql(
        f"SELECT ?c ?lei WHERE {{ VALUES ?c {{ {values} }} ?c wdt:P1278 ?lei }}"
    )
    return {qid(r["c"]["value"]): r["lei"]["value"] for r in rows}


# Indexes and funds can carry an exchange statement too; Wikidata's "instance of"
# lookup that would exclude them is too slow for the public endpoint, so filter by name.
_NOT_A_COMPANY = re.compile(r"\b(index|indices|etf)\b", re.IGNORECASE)


def is_placeholder(label: str | None) -> bool:
    """Wikidata returns the bare QID when an item has no English label."""
    return not label or (label[0] == "Q" and label[1:].isdigit())


def home_countries(
    country_rows: dict[str, list[dict]], qids: dict[str, str]
) -> dict[str, str]:
    """Pick one country per company: where its headquarters is, else the first listed.

    A company with several P17 values appears in every one of those countries'
    results; without this it would land in whichever ISO code sorts first.
    """
    home: dict[str, str] = {}
    for iso, rows in country_rows.items():
        for row in rows:
            q = qid(row["c"]["value"])
            hq_country = val(row, "hqCountry")
            if hq_country and qid(hq_country) == qids[iso]:
                home[q] = iso
            else:
                home.setdefault(q, iso)
    return home


def collect(
    country_rows: dict[str, list[dict]],
    countries: dict[str, str],
    qids: dict[str, str],
) -> dict:
    """Merge per-country rows into one record per company."""
    home = home_countries(country_rows, qids)
    companies: dict[str, dict] = {}
    for rows in country_rows.values():
        for row in rows:
            q = qid(row["c"]["value"])
            iso = home[q]
            name = val(row, "cLabel")
            if is_placeholder(name) or _NOT_A_COMPANY.search(name or ""):
                continue
            rec = companies.setdefault(
                q,
                {
                    "wikidata_id": q,
                    "canonical_name": name,
                    "parent_company": None,
                    "headquarters": None,
                    "country": iso,
                    "identifier": None,
                },
            )
            parent = val(row, "parentLabel")
            if parent and not is_placeholder(parent) and not rec["parent_company"]:
                rec["parent_company"] = parent
            hq = val(row, "hqLabel")
            if hq and not is_placeholder(hq) and not rec["headquarters"]:
                rec["headquarters"] = f"{hq}, {countries[iso]}"
    for rec in companies.values():
        if not rec["headquarters"]:
            rec["headquarters"] = countries[rec["country"]]
    return companies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=Path("data/companies.full.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/.wikidata-cache"))
    parser.add_argument(
        "--countries", nargs="*", help="ISO codes to build (default: every country)"
    )
    args = parser.parse_args()

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    everything = fetch_countries()
    wanted = {c.upper() for c in args.countries} if args.countries else None
    chosen = [t for t in everything if wanted is None or t[1] in wanted]
    names = {iso: name for _, iso, name in everything}
    print(f"{len(chosen)} countries")

    country_rows: dict[str, list[dict]] = {}
    for n, (country_qid, iso, name) in enumerate(chosen, 1):
        cache = args.cache_dir / f"listed-{iso}.json"
        if cache.exists():
            rows = json.loads(cache.read_text(encoding="utf-8"))
        else:
            try:
                rows = fetch_listed(country_qid)
            except Exception as err:  # keep going; rerun fills the gap from cache
                print(
                    f"[{n}/{len(chosen)}] {iso} {name}: FAILED {err!r}", file=sys.stderr
                )
                continue
            cache.write_text(json.dumps(rows), encoding="utf-8")
            time.sleep(PAUSE_SECONDS)
        country_rows[iso] = rows
        print(f"[{n}/{len(chosen)}] {iso} {name}: {len(rows)} rows")

    companies = collect(
        country_rows, names, {iso: country_qid for country_qid, iso, _ in everything}
    )
    print(f"{len(companies)} companies; fetching aliases")

    ids = sorted(companies)
    aliases: dict[str, list[str]] = {}
    for start in range(0, len(ids), ALIAS_BATCH):
        batch = ids[start : start + ALIAS_BATCH]
        cache = args.cache_dir / f"aliases-{batch[0]}-{len(batch)}.json"
        if cache.exists():
            found = json.loads(cache.read_text(encoding="utf-8"))
        else:
            found = fetch_aliases(batch)
            cache.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
            time.sleep(PAUSE_SECONDS)
        aliases.update(found)
        print(f"  aliases {min(start + ALIAS_BATCH, len(ids))}/{len(ids)}")

    for start in range(0, len(ids), ALIAS_BATCH):
        batch = ids[start : start + ALIAS_BATCH]
        cache = args.cache_dir / f"leis-{batch[0]}-{len(batch)}.json"
        if cache.exists():
            leis = json.loads(cache.read_text(encoding="utf-8"))
        else:
            leis = fetch_leis(batch)
            cache.write_text(json.dumps(leis), encoding="utf-8")
            time.sleep(PAUSE_SECONDS)
        for q, lei in leis.items():
            companies[q]["identifier"] = lei
        print(f"  leis {min(start + ALIAS_BATCH, len(ids))}/{len(ids)}")

    records = []
    for q in ids:
        rec = companies[q]
        seen: dict[str, None] = {rec["canonical_name"]: None}
        for alias in aliases.get(q, []):
            seen.setdefault(alias, None)
        records.append({**rec, "aliases": list(seen)})

    args.out.write_text(
        json.dumps(records, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    meta = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "Wikidata SPARQL (listed companies: P414 without end date)",
        "countries": len(chosen),
        "companies": len(records),
        "aliases": sum(len(r["aliases"]) for r in records),
        "with_lei": sum(1 for r in records if r["identifier"]),
    }
    args.out.with_suffix(".meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
