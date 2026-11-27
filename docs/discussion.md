---
title: "Entity Resolver: Discussion"
author: "Group 4: Yuhua Pang, Ken Qin, Norman Wang, Hongjin Zeng"
date: "DSAN 6700, Georgetown University, December 2026"
mainfont: "Times New Roman"
monofont: "Times New Roman"
fontsize: 12pt
geometry: margin=1in
linestretch: 1
documentclass: article
classoption: [onecolumn]
header-includes: |
  ```{=latex}
  \usepackage{titling}
  \usepackage{titlesec}
  \titlespacing*{\section}{0pt}{6pt}{2pt}
  \titleformat*{\section}{\normalsize\bfseries}
  \setlength{\parskip}{3pt}
  \setlength{\parindent}{0pt}
  \usepackage{etoolbox}
  \AtBeginEnvironment{longtable}{\small}
  \pagestyle{plain}
  \usepackage{xeCJK}
  \setCJKmainfont{Songti SC}
  \xeCJKDeclareCharClass{Default}{"2018,"2019,"201C,"201D}
  \usepackage{xurl}
  \setlength{\droptitle}{-4em}
  \sloppy
  ```
---

<!-- Source for discussion.pdf. Rebuild from the repository root:
pandoc docs/discussion.md -o discussion.pdf --pdf-engine=xelatex
-->

# 1. The user and the problem

An analyst joining vendor, contract or news data meets the same company written ten
ways: "Wal-Mart Stores Inc.", "Walmart", "沃尔玛". A join on the raw text silently
splits one supplier into many, and a fuzzy join silently merges different ones. Our
service takes one messy name, or a spreadsheet column of them, and returns one stable
answer per company: an English canonical name, the Wikidata id to join on, the parent
company and the headquarters. Just as important, it says when it is unsure
(`needs_review`), when it does not know (`unresolved`), and when it cannot answer right
now (`503` with `Retry-After`, or a `retry` row in a spreadsheet) instead of guessing.
For this user a confident wrong answer is the worst outcome: it looks verified and goes
straight into a join. That principle drove most of the decisions below.

# 2. Architecture

The live service runs on one Jetstream2 instance with Docker Compose, at
<https://team-4-final.cis240466.projects.jetstream-cloud.org>. **Caddy** is the only
container with published ports (80 and 443). It obtains the Let's Encrypt
certificate, redirects HTTP to HTTPS, adds security headers, caps request bodies
(12 MB uploads, 512 KB JSON) and hides `/metrics` from the internet. Behind it, one
**FastAPI** process scores names with RapidFuzz against a local alias index of 14,127
listed companies with multilingual aliases, built from Wikidata. The index lives in a
Docker volume, not in the image. Scoring takes about 1.3 ms, so the "model" runs as a
library in the API process, called through `asyncio.to_thread` so that `/health` stays
responsive. When a name is not in the index (brands and subsidiaries such as YouTube),
the API asks the **Wikidata API**. Every call there has a time limit we chose: 5 s per
call, a 15 s deadline per lookup, at most four lookups at once, retries with
exponential backoff and jitter for timeouts and 5xx, a single wait for a short
`Retry-After` on a 429, and a circuit breaker that fails fast for 30 s after five
failures. Spreadsheet uploads (.xlsx, .xls, .csv; 10 MB and 5,000 rows) run as
background jobs; the page polls their progress and the result can be downloaded with
the answers appended. **Prometheus** scrapes `/metrics` with a bearer key on the
internal network, and **Grafana** (loopback only, reached over an SSH tunnel) shows
request rates, errors, latency, resolution outcomes, Wikidata call time and cache
hits. Every setting comes from the environment through typed Pydantic Settings that
fail fast at startup; the production profile refuses to start without an API key and
JSON logs.

Shipping is scripted end to end. Every push runs ruff, mypy, pytest (290 tests),
gitleaks and an image build that is smoke-tested against fixed answers. Commits on
`main` are published to the GitHub Container Registry under their commit SHA, and a
`v*` tag retags that image as the release. On the instance, `scripts/deploy.sh 0.2.0`
pulls the release, records its digest, starts it, waits for health, checks a fixed set
of known answers and appends a line to a deploy log; `deploy.sh --previous` rolls back
to the last good version. Diagrams of the process, execution, state and technical
views, and where each time limit sits, are in `docs/architecture.md`.

# 3. Baseline, improvements and how we measured success

We measured accuracy on a labeled set of 209 queries (`eval/`): messy variants, typos,
legal suffixes, multilingual aliases, ambiguous names with a region, private companies
and gibberish that must stay unresolved, and, after user testing, junk placeholders,
same-name companies and non-companies.

| Method (176 original queries) | Accuracy | Wrong | Unlisted left unresolved |
|------------------------------|---------|-------|----------------|
| Exact match on normalized alias (baseline) | 79.9% | 0.7% | 100.0% |
| RapidFuzz `WRatio` at 0.80 (first default) | 95.8% | 4.2% | 9.4% |
| RapidFuzz `ratio` at 0.90 (current) | 94.4% | 0.7% | 100.0% |

`WRatio` looked best on accuracy, but its partial-substring score matched almost any
short name to something ("instagram" resolved to Norfolk Southern Railway), so it left
only 9.4% of unlisted names unresolved. We switched to plain `ratio` at 0.90 and
accepted the cost: about 4.9% of true matches, mostly typos, are missed. Through the
full service path (junk guard, short-name rule, same-name tie-break) the current system
scores 94.8% on all 209 queries with 0.6% wrong and 98.1% of should-not-match queries
left unresolved. With the live Wikidata fallback, non-companies left unresolved went
from 1 of 10 to 7 of 10 after the last round of fixes.

For the running service, success means a service level objective: at least 95% of
`POST /resolve` requests finish within 1 second over 30 days, tracked in Grafana next
to the error rate. After the reboot drill, the known-answer check over HTTPS answered
17 queries with p50 134 ms and p95 226 ms. The same check runs after every deploy, so a
release that answers wrongly fails even when every status code is 200.

# 4. What our testers found and what we did

Our Homework 2 and 3 testers filed 55 issues. Every one now has a response in
`docs/tester-responses.md`: 34 led to a fix (fully or in part), and the rest were
answered or closed as a documented decision not to fix. The findings that mattered
most were all the same failure, confident wrong answers, which our own evaluation had
missed because it never tried the inputs real users type:

- **Same-name companies (#33).** "Tesco PLC" and "Meta" returned a different company
  with the same name at 100%. Ties now go to the top-level company, then the lowest
  Wikidata number, and are flagged `needs_review`.
- **Junk and placeholders (#75, #85).** An emoji resolved to Bitcoin, "T" to AT&T,
  and "N/A" to an electrical substation, because symbol-only input normalized to an
  empty string that matched everything. Junk, single letters and placeholders now stay
  unresolved, and short names must match exactly.
- **Non-companies (#78).** A university, the United Nations and a skyscraper resolved
  as companies through Wikidata. We skip non-company classes and no longer let a
  related entity (a university press) stand in for a skipped one.
- **Silent partial results (#76).** A spreadsheet where most rows were never looked up
  still ended with a green "Done". Rows that could not be looked up are now marked
  `retry`, and a re-run redoes only those rows.

Some findings we chose not to fix, with reasons: headquarters values are shown as
Wikidata stores them (#38), because cleaning them would mean a second data source.

# 5. Decisions, failures and lessons

**Say "I don't know".** Each accuracy decision traded a little recall for fewer
confident errors: `ratio` over `WRatio`, unresolved over a weak match, `needs_review`
on ties, and `503` when Wikidata is down rather than a stale or partial answer.

**Test the inputs users actually type.** Our first evaluation reported 0.7% wrong, and
testers still found dozens of wrong answers in an afternoon. We added their inputs to
the evaluation as permanent categories, so those regressions are now measured.

**Run the procedures, not just the tests.** The old instance's 20 GB disk filled up,
so we did a real fresh install on a new instance. It exposed a bug that 290 tests and
CI had not: the new index volume was owned by root, so restoring the index failed with
`Permission denied` (postmortem in `docs/postmortem-fresh-install.md`, fixed in #126).
The same session restored the index from a backup kept off the instance, deployed
v0.2.0, rolled back with one command and rebooted the instance; everything came back
on its own. The results are in `docs/runbook.md`.

**Know your dependency.** Wikidata throttles bursts. Four deploys within a minute made
every second known-answer check see `503`s, while the same check run alone passed. The
service handles this as designed (local answers keep working and the `503` carries
`Retry-After`), but it taught us to space out deploys and to read a `WARN` before
treating it as a regression.

**Security and scale.** Routes that are not for every user check who is calling:
`/resolve/batch` and `/metrics` need the API key (401 without it), and a user can see
only their own upload jobs (anyone else gets 404). Expensive routes are rate-limited
with `429` and `Retry-After`, secrets stay out of the repository, image and logs, and
every base image and GitHub Action is pinned. The application has no language model,
so untrusted text in uploads cannot steer a model into sending data out. If two copies
ran behind one address, the in-memory state would break: upload jobs, rate-limit
counters, the Wikidata cache and the circuit breaker. `docs/scaling.md` describes
moving that state to Redis and object storage and running the API as a Kubernetes
Deployment.
