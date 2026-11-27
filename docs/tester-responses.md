# Responses to tester issues

Every issue opened by our Homework 2 and Homework 3 testers, with what we did about
it: **Fixed** (the PR that fixed it), **Won't fix** (and why), or **Answered** (where
the answer lives). Tracking issue: [#104](https://github.com/normangw/entity-resolver/issues/104).

Each "Fixed" row was re-checked on `main` on 2026-11-25 against the full 14,127-company
index, with the Wikidata fallback on, using the tester's own inputs.

Two notes on the PR numbers:

- **#93 was closed without merging.** Its commits landed in `main` through
  [#95](https://github.com/normangw/entity-resolver/pull/95) (commit `443dca4`), which
  was built on top of it. Earlier comments that say "closes when #93 merges" refer to
  code that is now in `main` through #95.
- **Follow-up fixes from this pass** (the last three rows of the summary below) are in
  their own PRs, one per fix, linked from the table.

| PR | What |
|---|---|
| [#67](https://github.com/normangw/entity-resolver/pull/67) | Short names must match exactly; generic words and legal forms never resolve; `needs_review` |
| [#68](https://github.com/normangw/entity-resolver/pull/68) | Upload fixes: encodings, delimiters, headers, retry rows, expiry |
| [#69](https://github.com/normangw/entity-resolver/pull/69) | English canonical name with a translated `label`; Region and Language wording |
| [#70](https://github.com/normangw/entity-resolver/pull/70) | README "Design decisions and known limits"; clearer Country and Parent wording |
| [#95](https://github.com/normangw/entity-resolver/pull/95) (includes #93) | Junk input, same-name tie-breaks, noisy names, review flags in spreadsheets, `matched_alias`, `as_of`, `note`, Wikidata in English with 25 hits, non-company classes skipped, page wording |
| [#94](https://github.com/normangw/entity-resolver/pull/94) | README answers for the confidence, split-company, brand, alias, as-of and API questions |
| [#122](https://github.com/normangw/entity-resolver/pull/122) | `N/A`, `#N/A` and `n.a.` cells are placeholders, not a Wikidata search (#85 follow-up) |
| [#123](https://github.com/normangw/entity-resolver/pull/123) | "Time Warner" no longer resolves to Time Warner Cable (#84) |
| [#124](https://github.com/normangw/entity-resolver/pull/124) | A non-company name no longer falls through to a related hit (#78 follow-up) |

## Which issues we fixed on purpose in this pass, and why

Re-running every tester input on `main` found three that still gave a confident wrong
answer. We fixed those, and only those, because a wrong company at 100% is the failure
that costs the analyst in the brief the most: it looks like a verified match and goes
straight into a join.

1. **`N/A` resolved to an electrical substation at 100% (#85).** `normalize("N/A")` is
   `"n a"`, which never matched the placeholder list (it held `n` and `a` as separate
   words), so the cell went to Wikidata. Excel's `#N/A` error value, common in exported
   vendor columns, did the same. Placeholders are now compared with spaces removed.
2. **"Time Warner" resolved to Time Warner Cable (#84).** This was index data, not
   matching logic: Wikidata files "Time Warner" and "Time-Warner" as aliases of the
   cable company. Rebuilding the index takes about 20 minutes and would bring the same
   aliases back, so the wrong aliases are dropped when any index loads. The name now
   goes to Wikidata and returns WarnerMedia (Q191715), still flagged for review with
   the successor note.
3. **"georgetown university" resolved to Georgetown University Press, and "united
   nations" to the Security Council (#78).** #95 skipped the university and the UN
   themselves, so the next hit, a related entity with an owner, won. Now, if the name
   matches a skipped non-company better than any company, the answer is unresolved.
   The eval queries added in #116 also showed the EU, the World Bank, "City of
   Chicago" and "Red Cross" resolving; their Wikidata classes are now skipped. The
   `non_company` category goes from 1/10 to 7/10 unresolved with live Wikidata.

The other two candidates in #104 were already done: the Language setting no longer
changes `canonical_name` (#69; `Toyota` gives the same English name in `en`, `ja` and
`zh`), and the home page now says what is covered (#95).

## Bugs and UX issues

| Issue | Title | Response | Where |
|---|---|---|---|
| [#32](https://github.com/normangw/entity-resolver/issues/32) | Spreadsheet job can fail with a generic error and no partial results | **Fixed.** A busy Wikidata no longer fails the job: local matches are kept, the rest are marked `retry`, and a re-run only redoes the `retry` rows. | #68, #95 |
| [#33](https://github.com/normangw/entity-resolver/issues/33) | Well-known names resolve to a different, same-named company at 100% | **Fixed.** Same-name ties go to the top-level company, then the lowest Q-number, and are flagged. Verified: `Tesco PLC` → Tesco (Q487494), `McDonald's Corporation` → Q38076, `Meta` → Meta Platforms (Q380), `Siemens AG` → Siemens (Q81230), all with `needs_review`. | #67, #95 |
| [#34](https://github.com/normangw/entity-resolver/issues/34) | Different companies with similar spellings returned as resolved | **Fixed** (already closed). Names under 8 normalized characters must match exactly (`Lexus` no longer matches Plexus). | #67 |
| [#35](https://github.com/normangw/entity-resolver/issues/35) | Generic words, a legal suffix and place names get confident answers | **Fixed.** `Bank`, `Inc.`, `Paris`, `USA` and similar never resolve, and never reach Wikidata. | #67, #95 |
| [#36](https://github.com/normangw/entity-resolver/issues/36) | Choosing the correct region makes the brief's companies unresolved | **Fixed (wording); won't fix (behavior).** Wikidata results carry no country, so they cannot honor a region and the fallback runs only with World. The unresolved message now says so instead of "check the spelling", and the Region hint says to use World for companies outside the listed regions. | #69, #95 |
| [#37](https://github.com/normangw/entity-resolver/issues/37) | Same company gets a different canonical name by Language | **Fixed.** `canonical_name` is always English; the translated name is in `label`. | #69 |
| [#38](https://github.com/normangw/entity-resolver/issues/38) | Headquarters values contradictory, not a city, or in different formats | **Won't fix.** Headquarters is shown as Wikidata stores it; cleaning it means a second geocoding source, which is out of scope. The README says to treat it as a hint and `country` (local matches) as the reliable field. | README, #94 |
| [#40](https://github.com/normangw/entity-resolver/issues/40) | GBK CSV garbled, but "Original cells unchanged" passes | **Fixed.** UTF-8, UTF-16, GBK and Windows-1252 are detected; the page notes a non-UTF-8 file. | #68 |
| [#41](https://github.com/normangw/entity-resolver/issues/41) | Workbook loses other sheets, formulas and formatting | **Fixed (honesty); won't fix (round-trip).** The download is the chosen sheet as values, and the result and page now say so. Rewriting the workbook in place would need a separate xlsx writer per format. | #68 |
| [#52](https://github.com/normangw/entity-resolver/issues/52) | Semicolon-delimited CSV read as one column | **Fixed.** Comma, semicolon, tab and pipe are detected. | #68 |
| [#53](https://github.com/normangw/entity-resolver/issues/53) | CSV headers trimmed | **Fixed.** Header whitespace is kept. | #68 |
| [#54](https://github.com/normangw/entity-resolver/issues/54) | Existing "Resolver status" column gives duplicate headers | **Fixed.** Clashing result columns get a `(2)` suffix. | #68 |
| [#55](https://github.com/normangw/entity-resolver/issues/55) | Truncated UTF-16 CSV returns HTTP 500 | **Fixed.** A 400 asking to re-save as UTF-8. | #68 |
| [#56](https://github.com/normangw/entity-resolver/issues/56) | Expired upload still downloadable | **Fixed.** An expired job is deleted and returns 404 when requested. | #68 |
| [#62](https://github.com/normangw/entity-resolver/issues/62) | Chinese match returns none for country | **Answered; won't fix.** Wikidata matches carry no country; the page now says "not available from Wikidata lookup" instead of a bare "none". A country for Wikidata matches would need another lookup per result and is not built. | #70 |
| [#74](https://github.com/normangw/entity-resolver/issues/74) | "Mars" only resolves in Japanese or Spanish | **Fixed.** Wikidata is always searched in English with 25 hits. Verified: `Mars` → Mars, Incorporated (Q695087) in English and Japanese. | #95 |
| [#75](https://github.com/normangw/entity-resolver/issues/75) | Emoji or symbol-only input resolves to Bitcoin at 100% | **Fixed.** Fewer than two letters or digits is unresolved, and symbol-only aliases are dropped. Verified with 😀, `?`, `>`, `-`, `???`. | #95 |
| [#76](https://github.com/normangw/entity-resolver/issues/76) | Most rows left as "retry" but the job shows a green "Done" | **Fixed.** The job ends "Partly done" (amber) with the count, and a re-run only looks up the `retry` rows. | #95 |
| [#77](https://github.com/normangw/entity-resolver/issues/77) | Reloading during a job loses the upload with no warning | **Fixed (warning); won't fix (resume).** The page asks before you leave mid-job. Returning to a job after leaving is not built; see #47. | #95 |
| [#78](https://github.com/normangw/entity-resolver/issues/78) | Buildings, universities and international organizations resolve as companies | **Fixed.** Hits whose `instance of` is a building, university, international organization, agency or nonprofit are skipped (#95), and a related hit no longer takes their place, and the EU, World Bank, city-government and Red Cross classes are skipped too (this pass). On the `non_company` eval queries from #116 with live Wikidata, 7 of 10 are now unresolved (1 of 10 on `main`). The class list is not exhaustive, and `NASA` still matches a local ticker alias. | #95, #124 |
| [#79](https://github.com/normangw/entity-resolver/issues/79) | Column picker chooses "Contact Name" over "Supplier" | **Fixed.** Supplier, Vendor, Counterparty and Company headers win; ID, number, phone and person-name columns are not suggested; the page asks you to confirm. | #95 |
| [#80](https://github.com/normangw/entity-resolver/issues/80) | Home page doesn't say what the tool covers | **Fixed.** The page states coverage (about 14,000 listed companies plus Wikidata), what is not covered, and that unresolved means "not found in these sources". | #95 |
| [#82](https://github.com/normangw/entity-resolver/issues/82) | Spreadsheet results lose the "please review" warning | **Fixed.** A `Needs review` column in the table and download, and a flagged-row count in the summary. | #95 |
| [#83](https://github.com/normangw/entity-resolver/issues/83) | Wikidata errors show up as "Model still loading" | **Fixed.** A Wikidata 503 shows "Wikidata is busy"; "Model still loading" is only for the real loading case. Wikidata calls are also retried with backoff and capped in concurrency. | #95 |
| [#84](https://github.com/normangw/entity-resolver/issues/84) | "Time Warner" resolves to Time Warner Cable | **Fixed** (issue closed 2026-11-25 on the review flag; this pass also fixes the data). The wrong alias is dropped at index load; `Time Warner` and `Time Warner Inc.` now return WarnerMedia (Q191715), flagged for review with the successor note. `Time Warner Cable` and `TWC` still resolve to the cable company. | [#123](https://github.com/normangw/entity-resolver/pull/123) |
| [#85](https://github.com/normangw/entity-resolver/issues/85) | TBD, T, V, X resolve to companies | **Fixed.** `T`, `V`, `X` and `TBD` are unresolved (#95). This pass also fixes `N/A`, `#N/A`, `n.a.` and `NaN`, which were reaching Wikidata and returning an electrical substation. | #95, #122 |
| [#86](https://github.com/normangw/entity-resolver/issues/86) | CSV download adds apostrophes to phone numbers | **Fixed.** Phone numbers and a lone dash keep their text. Other cells starting with `=`, `+`, `-` or `@` still get an apostrophe as formula-injection protection; the README says so. | #95 |
| [#87](https://github.com/normangw/entity-resolver/issues/87) | Names with a city or vendor number don't resolve | **Fixed.** Retried without brackets and vendor numbers, then without a trailing city and state. Verified on all three examples. | #95 |

## Questions and design questions

| Issue | Title | Response | Where |
|---|---|---|---|
| [#39](https://github.com/normangw/entity-resolver/issues/39) | Parent company inconsistent, out of date, or blank | **Answered; won't fix.** Parent is one level up, as Wikidata records it, and blank means "none on record", not "independent". Group roll-up is not built. | README "Entity level", #70 |
| [#42](https://github.com/normangw/entity-resolver/issues/42) | Which field to join on; which index produced a result | **Answered.** Join on `wikidata_id`. `as_of` now gives the index build date (local) or lookup date (Wikidata). | README "Join key", #70, #95 |
| [#43](https://github.com/normangw/entity-resolver/issues/43) | What `confidence` means across two scorers | **Answered** (already closed). Name similarity, comparable within one `source`. | README `POST /resolve`, #94 |
| [#44](https://github.com/normangw/entity-resolver/issues/44) | Does the evaluation cover the live system? | **Answered.** No: `eval/` scores the local index on the set that picked the threshold, so treat it as a ceiling. A held-out set and a Wikidata-on run are listed as open in the postmortem. | README "Evaluation", `docs/postmortem-tester-findings.md` |
| [#45](https://github.com/normangw/entity-resolver/issues/45) | How to correct a wrong match so it stays corrected | **Won't fix.** Saved corrections need user accounts and storage, which this demo does not have. Fix the row in your copy. | README "Corrections", #70 |
| [#46](https://github.com/normangw/entity-resolver/issues/46) | Is the app meant for confidential vendor lists? | **Answered.** No. Unmatched names go to Wikidata, and uploads stay on the server for an hour. The brief and README say not to upload confidential lists. HTTPS and upload ownership are tracked in #100. | README "Confidential data", `USER_BRIEF.md`, #70 |
| [#47](https://github.com/normangw/entity-resolver/issues/47) | Resume an upload after refreshing | **Won't fix.** No accounts means no safe way to hand a job back to the right person. The page now warns before leaving. | README "Size", #95 |
| [#48](https://github.com/normangw/entity-resolver/issues/48) | Use the country column in a mixed-country file | **Won't fix.** One region per file. Use World for a mixed file; per-row region would also turn off the Wikidata fallback for those rows (#36). | README "Design decisions", #70 |
| [#49](https://github.com/normangw/entity-resolver/issues/49) | Company-group summary for deduplication | **Won't fix.** Group by `wikidata_id` in the download; a built-in roll-up is not planned. | README "Entity level", #70 |
| [#50](https://github.com/normangw/entity-resolver/issues/50) | Workflow for lists over 5,000 rows | **Answered.** Split the file. For scripts, `POST /resolve/batch` takes 100 names per call (and requires the API key when `API_KEY` is set). | README "Size", #95 |
| [#51](https://github.com/normangw/entity-resolver/issues/51) | Distinguish active from inactive entities | **Won't fix.** The index holds currently listed companies; a lifecycle field is not built. | README "History", #70 |
| [#57](https://github.com/normangw/entity-resolver/issues/57) | Consistent display language for multilingual results | **Fixed.** Canonical name is English; the translated name is `label`; parent and HQ follow the Language setting, as the hint says. | #69 |
| [#58](https://github.com/normangw/entity-resolver/issues/58) | Does it support the list-matching workflow? | **Answered.** Join on `wikidata_id`; filter on `needs_review`. | README "Join key", #70 |
| [#59](https://github.com/normangw/entity-resolver/issues/59) | What does 100% confidence mean? | **Answered.** The text matched a known name exactly; it is not a probability. The page labels it "Name similarity". | README `POST /resolve`, #94, #95 |
| [#60](https://github.com/normangw/entity-resolver/issues/60) | Preserve historical names for audit | **Answered.** `matched_alias` shows the stored name matched (for `Apple Computer`, the old name) and `as_of` dates the answer. The alias type and period are not stored. | #95, README |
| [#61](https://github.com/normangw/entity-resolver/issues/61) | How to use a high but sub-100% confidence | **Answered.** Treat any score below 1.0, or `needs_review: true`, as a row to check. | README `POST /resolve`, #94 |
| [#63](https://github.com/normangw/entity-resolver/issues/63) | Language selection unclear for English lookups | **Fixed.** A hint under Language says it only changes the names returned; matching is the same. | #69, #95 |
| [#64](https://github.com/normangw/entity-resolver/issues/64) | README `/resolve` example vs the live app | **Fixed.** The example matches the deployed response. | #70, #94 |
| [#65](https://github.com/normangw/entity-resolver/issues/65) | Google LLC: operating company or parent? | **Answered.** The index treats the group as the canonical company, so `Google LLC` → Alphabet Inc. | README "Entity level", #70 |
| [#66](https://github.com/normangw/entity-resolver/issues/66) | Spreadsheet vs single-name fields differ | **Fixed.** Both use the same resolver and English canonical name; the single-name card also shows `label`. | #69, README |
| [#73](https://github.com/normangw/entity-resolver/issues/73) | What does the "Confidence" percentage mean? | **Answered and fixed.** Renamed "Name similarity" on the page with a one-line explanation. | #94, #95 |
| [#81](https://github.com/normangw/entity-resolver/issues/81) | What does the Region filter mean? | **Fixed (hint); won't fix (more regions).** Region means headquarters country; the hint says to use World for others. | #95 |
| [#88](https://github.com/normangw/entity-resolver/issues/88) | What should "General Electric" resolve to after the split? | **Answered.** GE Aerospace (the renamed parent), flagged for review with a note naming GE Vernova and GE HealthCare; `Hewlett-Packard` is flagged the same way. | README "Split and renamed companies", #94, #95 |
| [#89](https://github.com/normangw/entity-resolver/issues/89) | How should brands and d/b/a names be handled? | **Answered.** A brand resolves to itself with its owner as parent when Wikidata records one; for "X d/b/a Y" the legal entity X is tried first. | README "Brands and d/b/a names", #94, #95 |
| [#90](https://github.com/normangw/entity-resolver/issues/90) | Can results show which alias matched? | **Fixed.** `matched_alias` (e.g. `Time-Warner` is how #84 was traced). The alias type is not stored. | #95 |
| [#91](https://github.com/normangw/entity-resolver/issues/91) | Can results include an "as of" date? | **Fixed.** `as_of` is the index build date or the Wikidata lookup date. Wikidata values (WarnerMedia's parent) can still be stale. | #95 |
| [#92](https://github.com/normangw/entity-resolver/issues/92) | Is the API meant to be used directly? | **Answered.** Yes, one name per `POST /resolve`. Scripts can use `POST /resolve/batch` (100 names per call; requires the API key when `API_KEY` is set). Per-client rate limits return 429 with `Retry-After`. | README, #95 |

## Not tester issues

#19, #20 and #21 are Deliverable 2 tasks, and #67 to #72 are our own PRs. They are not
covered here.
