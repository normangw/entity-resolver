"""Score the resolver against a labeled query set.

    uv run python scripts/evaluate.py --index data/companies.full.json \\
        --queries eval/queries.jsonl --ablate

Each query line is JSON: ``query``, ``expected`` (a Wikidata ID, or null when
nothing should match), ``category`` and an optional ``region``. For every scorer
and threshold it reports:

- accuracy: resolvable queries answered with the right company
- wrong: resolvable queries answered with a different company
- missed: resolvable queries left unresolved
- unresolved ok: should-not-match queries correctly left unresolved

Methods: the exact-match baseline (normalized query equals a normalized alias) and
RapidFuzz scorers over the alias index. Each scorer is run once per query and every
threshold is applied to that score. ``--detail scorer@threshold`` prints the
per-category breakdown for one setting; ``--ablate`` re-runs it with parts of the
preprocessing switched off.

The scorer table looks only at the best fuzzy score, so it skips what the running
service adds: the guard for junk and generic words, the short-name rule, the
same-name tie-break and the Wikidata fallback. ``--service`` sends every query
through ``POST /resolve`` of the app itself (in process, with the given index) and
prints each miss. ``--wikidata`` turns the live fallback on for that run, so
results depend on Wikidata at that moment.
"""

import argparse
import json
import os
import re
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from rapidfuzz import fuzz, process

from entity_resolver import resolver as rmod
from entity_resolver.resolver import Resolver

SCORERS: dict[str, Callable[..., float]] = {
    "WRatio": fuzz.WRatio,
    "ratio": fuzz.ratio,
    "token_sort_ratio": fuzz.token_sort_ratio,
}
THRESHOLDS = (0.6, 0.7, 0.8, 0.85, 0.9, 0.95)

Answer = tuple[str | None, float]  # (best company id, score 0-1); (None, 0) if none


def load_queries(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def best_answers(resolver: Resolver, queries: list[dict]) -> list[Answer]:
    """Best match per query, before any threshold is applied."""
    out: list[Answer] = []
    for q in queries:
        rows = resolver._region_rows[q.get("region", "world")]
        best = process.extractOne(
            rmod.normalize(q["query"]),
            {i: resolver._choices[i] for i in rows},
            scorer=resolver._scorer,
        )
        if best is None:
            out.append((None, 0.0))
        else:
            _, score, idx = best
            out.append((resolver._owners[idx].wikidata_id, score / 100))
    return out


def exact_answers(resolver: Resolver, queries: list[dict]) -> list[Answer]:
    """Baseline: the normalized query must equal a normalized alias exactly."""
    index: dict[tuple[str, str], str] = {}
    for region, rows in resolver._region_rows.items():
        for i in rows:
            index.setdefault(
                (region, resolver._choices[i]), resolver._owners[i].wikidata_id
            )
    out: list[Answer] = []
    for q in queries:
        hit = index.get((q.get("region", "world"), rmod.normalize(q["query"])))
        out.append((hit, 1.0) if hit else (None, 0.0))
    return out


def tally(
    queries: list[dict], answers: list[Answer], threshold: float
) -> dict[str, dict[str, int]]:
    """Outcomes per category (and "ALL") when only scores >= threshold count."""
    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for q, (got, score) in zip(queries, answers, strict=True):
        got = got if score >= threshold else None
        expected = q["expected"]
        if expected is None:
            outcome = "correct_unresolved" if got is None else "false_match"
        elif got == expected:
            outcome = "correct"
        elif got is None:
            outcome = "missed"
        else:
            outcome = "wrong"
        for key in (q["category"], "ALL"):
            result[key][outcome] += 1
            result[key]["n"] += 1
    return result


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def rates(t: dict[str, int]) -> tuple[str, str, str, str]:
    resolvable = t["correct"] + t["wrong"] + t["missed"]
    negatives = t["correct_unresolved"] + t["false_match"]
    return (
        pct(t["correct"], resolvable),
        pct(t["wrong"], resolvable),
        pct(t["missed"], resolvable),
        pct(t["correct_unresolved"], negatives),
    )


def print_detail(title: str, t: dict[str, dict[str, int]]) -> None:
    print(f"\n{title}")
    print(
        f"  {'category':<14}{'n':>5}{'accuracy':>10}{'wrong':>8}{'missed':>8}"
        f"{'unresolved ok':>15}"
    )
    for cat in sorted(t, key=lambda c: (c == "ALL", c)):
        acc, wrong, missed, neg = rates(t[cat])
        print(f"  {cat:<14}{t[cat]['n']:>5}{acc:>10}{wrong:>8}{missed:>8}{neg:>15}")


# --- preprocessing ablation --------------------------------------------------


def _lower_only(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower()).strip()


def _no_suffix_strip(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).lower().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_full = rmod.normalize


def _no_accent_fold(name: str) -> str:
    saved = rmod._fold_accents
    rmod._fold_accents = lambda text: text
    try:
        return _full(name)
    finally:
        rmod._fold_accents = saved


def ablate(index: Path, queries: list[dict], scorer: str, threshold: float) -> None:
    variants = {
        "full preprocessing": _full,
        "no accent folding": _no_accent_fold,
        "no suffix stripping": _no_suffix_strip,
        "lowercase only": _lower_only,
    }
    print(f"\nPreprocessing ablation ({scorer} at {threshold})")
    print(
        f"  {'variant':<22}{'accuracy':>10}{'wrong':>8}{'missed':>8}"
        f"{'unresolved ok':>15}"
    )
    for label, fn in variants.items():
        rmod.normalize = fn
        try:
            resolver = Resolver(index, scorer=SCORERS[scorer])
            answers = best_answers(resolver, queries)
        finally:
            rmod.normalize = _full
        acc, wrong, missed, neg = rates(tally(queries, answers, threshold)["ALL"])
        print(f"  {label:<22}{acc:>10}{wrong:>8}{missed:>8}{neg:>15}")


# --- the running service -----------------------------------------------------

# An answer id for a request the service failed (5xx); it never equals an expected
# id, so it counts as wrong, or as a false match for a should-not-match query.
_FAILED = "<service error>"


def service_answers(
    index: Path, queries: list[dict], wikidata: bool, pause: float = 0.0
) -> tuple[list[Answer], list[str]]:
    """Answers from ``POST /resolve`` of the app, and the source of each answer.

    The app is started in process with ``index`` as its alias index. Rate limits
    are off, ``wikidata`` turns the live fallback on or off, and ``pause`` seconds
    pass between queries so Wikidata does not throttle the run.
    """
    os.environ["ALIAS_INDEX_PATH"] = str(index.resolve())
    os.environ["WIKIDATA_FALLBACK"] = "true" if wikidata else "false"
    os.environ["RATE_LIMIT_RESOLVE_PER_MIN"] = "0"
    os.environ["GLEIF_ENABLED"] = "false"
    # Imported here: the app reads its settings at import time, from the variables
    # set just above.
    from fastapi.testclient import TestClient

    from entity_resolver.main import app

    answers: list[Answer] = []
    sources: list[str] = []
    with TestClient(app) as client:
        deadline = time.monotonic() + 300
        while client.get("/ready").status_code != 200:
            if time.monotonic() > deadline:
                raise SystemExit("the index did not load within 300 s")
            time.sleep(0.2)
        for n, q in enumerate(queries):
            if n and pause:
                time.sleep(pause)
            body = {"company_name": q["query"], "region": q.get("region", "world")}
            response = client.post("/resolve", json=body)
            if response.status_code >= 500:
                answers.append((_FAILED, 1.0))
                sources.append(f"HTTP {response.status_code}")
                continue
            data = response.json()
            if response.status_code != 200:  # a 422 for input the API rejects
                answers.append((None, 0.0))
                sources.append(f"HTTP {response.status_code}")
            elif data["status"] == "resolved":
                answers.append((data["wikidata_id"], 1.0))
                sources.append(f"{data['source']}: {data['canonical_name']}")
            else:
                answers.append((None, 0.0))
                sources.append("unresolved")
    return answers, sources


def print_misses(
    queries: list[dict], answers: list[Answer], sources: list[str]
) -> None:
    """List every query the service got wrong, with what it answered."""
    print("\nMisses")
    misses = 0
    for q, (got, _), source in zip(queries, answers, sources, strict=True):
        if got != q["expected"]:
            misses += 1
            want = q["expected"] or "unresolved"
            print(f"  [{q['category']}] {q['query']!r}: expected {want}, got {source}")
    if not misses:
        print("  none")


def main() -> None:
    """Parse the arguments and print the tables."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--queries", type=Path, default=Path("eval/queries.jsonl"))
    parser.add_argument("--detail", default="ratio@0.85", help="scorer@threshold")
    parser.add_argument("--ablate", action="store_true")
    parser.add_argument(
        "--service", action="store_true", help="score POST /resolve of the app"
    )
    parser.add_argument(
        "--wikidata", action="store_true", help="with --service: live fallback on"
    )
    parser.add_argument(
        "--categories", nargs="+", help="only score queries in these categories"
    )
    parser.add_argument(
        "--pause", type=float, default=0.0, help="with --service: seconds between"
    )
    args = parser.parse_args()

    queries = load_queries(args.queries)
    if args.categories:
        queries = [q for q in queries if q["category"] in args.categories]
    print(f"{len(queries)} queries, index {args.index}")
    if args.service:
        answers, sources = service_answers(
            args.index, queries, args.wikidata, args.pause
        )
        fallback = "on" if args.wikidata else "off"
        print_detail(
            f"POST /resolve, Wikidata fallback {fallback}", tally(queries, answers, 0.0)
        )
        print_misses(queries, answers, sources)
        return
    base = Resolver(args.index)

    exact = exact_answers(base, queries)
    print_detail(
        "Baseline: exact match on normalized alias", tally(queries, exact, 1.0)
    )

    print("\nAll queries, by scorer and threshold")
    print(
        f"  {'scorer':<18}{'thr':>5}{'accuracy':>10}{'wrong':>8}{'missed':>8}"
        f"{'unresolved ok':>15}"
    )
    all_answers: dict[str, list[Answer]] = {}
    for name, scorer in SCORERS.items():
        all_answers[name] = best_answers(Resolver(args.index, scorer=scorer), queries)
        for threshold in THRESHOLDS:
            acc, wrong, missed, neg = rates(
                tally(queries, all_answers[name], threshold)["ALL"]
            )
            print(f"  {name:<18}{threshold:>5}{acc:>10}{wrong:>8}{missed:>8}{neg:>15}")

    scorer, threshold = args.detail.split("@")
    print_detail(
        f"Per category: {scorer} at {threshold}",
        tally(queries, all_answers[scorer], float(threshold)),
    )
    if args.ablate:
        ablate(args.index, queries, scorer, float(threshold))


if __name__ == "__main__":
    main()
