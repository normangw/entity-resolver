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
| `WRatio` at 0.80 (current default) | 95.8% | 4.2% | 0.0% | 9.4% |
| `ratio` at 0.85 | 94.4% | 0.7% | 4.9% | 87.5% |
| `ratio` at 0.90 | 94.4% | 0.7% | 4.9% | 100.0% |

"Unresolved ok" is the share of `not_listed` queries correctly left unresolved.
`WRatio` includes a partial-substring score that returns 0.9 for almost any short
query on an index this size ("Cargill" matches "CyberAgent", "Koch Industries"
matches "S.T. Corp"), so no threshold below 0.95 rejects anything. Plain `ratio`
separates the cases. Full output: run the commands above.

Preprocessing ablation (`ratio` at 0.85): full 94.4%, no suffix stripping 90.3%,
lowercase only 88.2%. Accent folding changes nothing on this set.
