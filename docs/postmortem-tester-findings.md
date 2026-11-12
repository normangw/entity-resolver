# Postmortem: confident wrong answers found by user testing

Blameless write-up of what Homework 3 testing showed, how fast we responded, and what
we changed. Window: November 1 to 11, 2026. Testers filed 60+ issues (#32 to #92).

## Summary

The resolver's job is to return the right company or say it does not know. In testing
it often returned a wrong company with 100% confidence and no warning. Our own
evaluation (176 queries, 94.4% accuracy, 0.7% wrong, 100% of unlisted names left
unresolved) had not shown this, because it never tried the inputs testers used. No
customer data was affected; this was a class demo.

## What happened

| Finding | Issue | What the user saw |
|---|---|---|
| Same-name companies | #33 | "Tesco PLC", "McDonald's Corporation", "Meta" returned a different company with the same name, at 100% |
| Symbols and emoji | #75, #82 | An emoji, `?` or `-` resolved to Bitcoin at 100%, and in a spreadsheet looked like a normal match |
| Single letters and placeholders | #85 | `T`, `V`, `X` resolved to AT&T, Visa and X; `TBD` to a TV network |
| Wrong alias | #84 | "Time Warner" resolved to Time Warner Cable |
| Look-alike names | #34, #35 | Different companies with similar spellings, generic words and place names resolved as companies |
| Non-companies | #78 | A skyscraper, a university and the United Nations resolved as companies |
| Silent partial results | #76 | A spreadsheet where 91% of rows were never looked up still ended with a green "Done" |

## Why

- **Empty strings match each other.** Names are normalized by dropping symbols, so a
  symbol-only input became an empty string. RapidFuzz scores two empty strings as
  100, and an alias that normalized to empty (Bitcoin's symbol) matched everything
  of that kind. The short-name rule only blocked scores below 100.
- **Ties were arbitrary.** When several companies shared a name, the winner was
  decided by index order, and the result carried no warning.
- **Short exact names are real aliases.** `T` and `V` are genuine tickers in the index,
  so exact matching accepted them.
- **The Wikidata fallback accepted anything with an owner or a location,** including
  buildings and universities, and searched only 8 hits in the selected language.
- **One index alias was attached to the wrong record** (#84). This is a data problem
  and is not fixed in the index.
- **A job counted finished work, not correct work.** The summary reported "Done" and
  "all checks passed" while most rows were `retry`.
- **The evaluation measured the wrong thing.** It checked typos, variants and
  unlisted names, but had no junk input, same-name, or non-company categories, and
  the 0.90 threshold was tuned on the same queries it reports.

## How it was found and how fast

Found by external testers, not by our tests, monitoring or evaluation. The first fix
(#67, matching quality) merged on November 6, uploads and region/language fixes
(#68, #69) and the design answers (#70) on November 7, and the remaining findings
(#93, folded into #95) on November 11, the last day of the window.

## What we changed

- Input with fewer than two letters or digits, symbol-only input and placeholders
  are unresolved; aliases that normalize to nothing are dropped (#75, #85).
- Ties go to a top-level company, then the lowest Wikidata Q-number, and are flagged
  for review (#33).
- Names are retried without vendor numbers, brackets and a trailing city (#87).
- Split or merged names carry a review note (#84, #88).
- Wikidata results skip buildings, universities and similar classes (#78).
- Results show the matched alias and an as-of date; spreadsheets get a "Needs review"
  column; incomplete jobs end "Partly done" (#76, #82, #90, #91).
- The page now says what the score means and what the tool covers (#43, #59, #61, #73,
  #80).

## What went well

- Testers wrote reproducible findings, several with the code-level cause.
- The integrity checks (row count, cells unchanged) caught nothing wrong, and the
  review flag did what it was designed to do when it fired.
- Fixes shipped with tests, and the evaluation numbers stayed the same.

## What went badly

- We learned about confident wrong answers from users, not from us.
- Several fixes landed late in the window, so testers saw the old behavior for most
  of it.
- The Jetstream address changed during the window, which breaks links in the brief
  and the report.
- The index data problem (#84) and Wikidata headquarters and parent quality (#38) are
  still open.

## Action items

| Action | State |
|---|---|
| Guard empty, symbol and placeholder input | Done (#95) |
| Flag ties and uncertain names for review, in the UI and spreadsheets | Done (#95) |
| Scheduled uptime and canary check (`scripts/monitor.py`, `uptime.yml`) | Done |
| Back up the alias index and test a restore (`scripts/backup_index.py`) | Done |
| Add junk-input, same-name and non-company queries to `eval/` so this class of failure shows up in our own numbers | Open |
| Fix the Time Warner alias and clean headquarters and parent data in `build_index.py` | Open |
| Hold out part of the evaluation set so the threshold is not tuned on what it reports | Open |
