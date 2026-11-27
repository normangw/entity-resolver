# Evaluation set

`queries.jsonl` is the labeled query set; `scripts/evaluate.py` scores the resolver
against it. Both need the full index, which is built (not committed) with:

```bash
python scripts/build_index.py                      # ~20 min, writes data/companies.full.json
python scripts/make_eval_set.py --index data/companies.full.json   # rewrites queries.jsonl
python scripts/evaluate.py --index data/companies.full.json --ablate
python scripts/evaluate.py --index data/companies.full.json --service    # the real /resolve path
```

Run the evaluation with `PYTHONPATH=src` or after `uv sync` so `entity_resolver` imports.
`make_eval_set.py` rewrites the generated categories only; the `junk`, `same_name` and
`non_company` lines at the end of `queries.jsonl` are hand-written, so keep them if you
regenerate the file.

## Categories (209 queries)

| Category | n | How it is made | Limits |
|---|---|---|---|
| `variant` | 39 | Hand-written messy forms of well-known listed companies ("Wal-Mart Stores, Inc.") | Review these; labels are checked against the index |
| `typo` | 30 | One deleted, swapped or doubled letter in a canonical name (seed 0) | Synthetic |
| `suffix` | 25 | Canonical name in capitals plus a legal suffix | Synthetic |
| `multilingual` | 30 | A non-ASCII alias from the index used as the query | Synthetic and verbatim, so an exact match always succeeds |
| `ambiguous` | 20 | An alias shared by companies in different countries, queried with a `region` | Synthetic and verbatim |
| `not_listed` | 32 | Private companies and gibberish that must stay unresolved | Names that exactly match an index alias are dropped |
| `junk` | 12 | Placeholders from real vendor columns and Homework 3 tests (`TBD`, `N/A`, `T`, an emoji) | Hand-written; must stay unresolved |
| `same_name` | 11 | A name several index companies share (Tesco, Siemens AG, Broadcom, Shell) | Hand-written; the label is the company a user almost always means |
| `non_company` | 10 | Universities, international bodies, a building, a city (Georgetown University, United Nations) | Hand-written; must stay unresolved; kept even when one matches an index alias |

The verbatim categories (`multilingual`, `ambiguous`) and `suffix` score 100% for
every method. They check that nothing is broken; they do not discriminate between
scorers. `variant`, `typo` and `not_listed` carry the signal. The last three
categories (added for #112) test the guards in the service, so read them in the
"Service path" table below, not the scorer table.

## Results (index built 2026-10-25, 14,127 listed companies)

Re-run on 2026-11-25 with all 209 queries. The table scores only the best fuzzy
match per query, as before. On the 176 earlier queries the numbers are the same as
in the previous run.

| Method | Accuracy | Wrong | Missed | Unresolved ok | All 209: accuracy | All 209: unresolved ok |
|---|---|---|---|---|---|---|
| Exact match on normalized alias (baseline) | 79.9% | 0.7% | 19.4% | 100.0% | 76.8% | 96.3% |
| `WRatio` at 0.80 (previous default) | 95.8% | 4.2% | 0.0% | 9.4% | 91.6% | 13.0% |
| `ratio` at 0.85 | 94.4% | 0.7% | 4.9% | 87.5% | 90.3% | 85.2% |
| `ratio` at 0.90 (current default) | 94.4% | 0.7% | 4.9% | 100.0% | 90.3% | 96.3% |

The first four number columns are the 176 earlier queries; the last two include the
33 new ones. "Unresolved ok" is the share of should-not-match queries (`not_listed`,
and in the last column also `junk` and `non_company`) correctly left unresolved.
In this table `same_name` scores only 36.4%: it takes whichever tied alias RapidFuzz
returns first and skips the service's tie-break, so it understates the service.
`WRatio` includes a partial-substring score that returns 0.9 for almost any short
query on an index this size ("Cargill" matches "CyberAgent", "Koch Industries"
matches "S.T. Corp"), so no threshold below 0.95 rejects anything. Plain `ratio`
separates the cases. Full output: run the commands above.

## Service path (`--service`), 2026-11-25

`--service` sends each query through `POST /resolve` of the app itself, so the
junk guard, the short-name rule, the same-name tie-break and (with `--wikidata`)
the live Wikidata fallback all apply. These are what a user gets.

```bash
python scripts/evaluate.py --index data/companies.full.json --service
python scripts/evaluate.py --index data/companies.full.json --service --wikidata \
    --pause 20 --categories junk same_name non_company
```

| Category | n | Local index only | With the Wikidata fallback |
|---|---|---|---|
| `variant` | 39 | 100.0% | not run |
| `typo` | 30 | 76.7% (7 missed, 0 wrong) | not run |
| `suffix`, `multilingual`, `ambiguous` | 75 | 100.0% | not run |
| `not_listed` (unresolved ok) | 32 | 100.0% | not run (by design many resolve on Wikidata) |
| `junk` (unresolved ok) | 12 | 100.0% | 83.3% (10 of 12) |
| `same_name` | 11 | 90.9% (10 of 11) | 90.9% |
| `non_company` (unresolved ok) | 10 | 90.0% (9 of 10) | **10.0% (1 of 10)** |
| All 209, local only | 209 | 94.8% accuracy, 0.6% wrong, 4.5% missed, 98.1% unresolved ok | |

The Wikidata run covers only the three new categories (33 queries), with 20 s
between queries; the first two attempts the same day got `503` from Wikidata
throttling on most queries, and those runs are not counted. Wikidata answers
change over time, so this column is a snapshot.

**Failures, as found:**
- `N/A` and `n/a` resolve through Wikidata to "Tomlinson Street electrical
  substation". The junk guard in `resolver.py` builds `_GENERIC_NAMES` with
  `.split()`, so the two-word entry `"n a"` is never in the set and the guard misses
  it. Known bug, not fixed here (see #85).
- `non_company`: with Wikidata on, 9 of 10 resolve to something. Georgetown
  University and Harvard University go to their university presses, United Nations
  to the Security Council, World Health Organization to its Bulletin, Red Cross to
  the American Red Cross, and European Union, City of Chicago and World Bank to
  themselves. Only Empire State Building is blocked by the "not a company" class
  list (#78). The class list in `wikidata.py` is not exhaustive, as the README says,
  and these results show it misses most non-companies that have an owner or a
  headquarters on Wikidata.
- `NASA` resolves locally to Ayana Land International, an Indonesian listed company
  whose ticker alias is `NASA`. Short names must match exactly, and this one does.
- `Samsung` resolves to Samsung Group (Q20716), not Samsung Electronics (Q20718).
  "Samsung Group" normalizes to "samsung" (`group` is a legal suffix), so both tie
  and the lower Q-number wins. Samsung Group is a defensible answer; the label here
  says what most users of a supplier list mean.
- The 7 `typo` misses are the known cost of `ratio` at 0.90 (see below).

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
