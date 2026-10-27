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
"""

import argparse
import json
import re
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--queries", type=Path, default=Path("eval/queries.jsonl"))
    parser.add_argument("--detail", default="ratio@0.85", help="scorer@threshold")
    parser.add_argument("--ablate", action="store_true")
    args = parser.parse_args()

    queries = load_queries(args.queries)
    print(f"{len(queries)} queries, index {args.index}")
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
