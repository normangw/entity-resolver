# Evaluation set

`queries.jsonl` is the labeled query set; `scripts/evaluate.py` scores the resolver
against it. Both need the full index, which is built (not committed) with:

```bash
python scripts/build_index.py                      # ~20 min, writes data/companies.full.json
python scripts/make_eval_set.py --index data/companies.full.json   # rewrites queries.jsonl
python scripts/evaluate.py --index data/companies.full.json --ablate
```

Run the evaluation with `PYTHONPATH=src` or after `uv sync` so `entity_resolver` imports.

## Categories (176 queries)

| Category | n | How it is made | Limits |
|---|---|---|---|
| `variant` | 39 | Hand-written messy forms of well-known listed companies ("Wal-Mart Stores, Inc.") | Review these; labels are checked against the index |
| `typo` | 30 | One deleted, swapped or doubled letter in a canonical name (seed 0) | Synthetic |
| `suffix` | 25 | Canonical name in capitals plus a legal suffix | Synthetic |
| `multilingual` | 30 | A non-ASCII alias from the index used as the query | Synthetic and verbatim, so an exact match always succeeds |
| `ambiguous` | 20 | An alias shared by companies in different countries, queried with a `region` | Synthetic and verbatim |
| `not_listed` | 32 | Private companies and gibberish that must stay unresolved | Names that exactly match an index alias are dropped |

The verbatim categories (`multilingual`, `ambiguous`) and `suffix` score 100% for
every method. They check that nothing is broken; they do not discriminate between
scorers. `variant`, `typo` and `not_listed` carry the signal.

## Results (index built 2026-10-25, 14,127 listed companies)

| Method | Accuracy | Wrong | Missed | Unresolved ok |
|---|---|---|---|---|
| Exact match on normalized alias (baseline) | 79.9% | 0.7% | 19.4% | 100.0% |
| `WRatio` at 0.80 (previous default) | 95.8% | 4.2% | 0.0% | 9.4% |
| `ratio` at 0.85 | 94.4% | 0.7% | 4.9% | 87.5% |
| `ratio` at 0.90 (current default) | 94.4% | 0.7% | 4.9% | 100.0% |

"Unresolved ok" is the share of `not_listed` queries correctly left unresolved.
`WRatio` includes a partial-substring score that returns 0.9 for almost any short
query on an index this size ("Cargill" matches "CyberAgent", "Koch Industries"
matches "S.T. Corp"), so no threshold below 0.95 rejects anything. Plain `ratio`
separates the cases. Full output: run the commands above.

## Decision and known limitation

**Decision:** the default scorer is plain `ratio` at a 0.90 threshold. `WRatio` at 0.80
scored higher overall (95.8% against 94.4%), but only because it almost never leaves a
name unresolved: it correctly rejected 9.4% of unlisted names, so a private company or
gibberish usually came back as a wrong match with a confident score (for example
"Instagram" -> "S.T. Corp", "YouTube" -> "UTair Aviation"). The index holds listed
companies only, so most subsidiaries and brands are not in it, and a wrong answer
there is worse than no answer. `ratio` at 0.90 rejects all of them on this set (at 0.85, 87.5%; "Beats Electronics" matched "Bharat Electronics" at 0.857) with the same accuracy. The
tiebreak verifier does not help with `WRatio`, because it only runs on low-confidence
results and these are not low-confidence. Switching back is the `scorer=` argument of
`Resolver` and `MATCH_THRESHOLD`.

**Limitation:** `ratio` at 0.90 misses 4.9% of real matches, mostly typos (23.3% of the
typo category). The `not_listed` set is small (32 names), so these percentages are
rough. A private subsidiary resolves only if it has its own entry in the index (for
example Audi, Whole Foods); otherwise it stays unresolved.

Preprocessing ablation (`ratio` at 0.85): full 94.4%, no suffix stripping 90.3%,
lowercase only 88.2%. Accent folding changes nothing on this set.
