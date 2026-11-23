# Handoff: Deliverable 3 work in progress

Read this first when picking the work up on another machine. The full approved plan
is [docs/plans/d3-plan.md](docs/plans/d3-plan.md). Last updated 2026-11-23.

## Owners and issues

| Workstream | Issue | Owner |
|---|---|---|
| A: CI, registry, release | #98 | Norman (normangw) |
| B: index on a volume, deploy and rollback script, known-good checks | #99 | Norman |
| C: HTTPS with Caddy, Cache-Control, upload ownership, auth | #100 | Norman |
| E: settings profiles, model swap by .env, 429 retry, time limits | #101 | Norman |
| D: observability (loguru, Prometheus metrics, Grafana, SLO) | #102 | Hongjin Zeng (hz475-byte) |
| F: docstrings on every function, method and class | #103 | pangy8869898 |
| H: respond to every Homework 3 tester issue (and implement the chosen fixes in `src/`) | #104 | Ken Qin (kenqin0221) |
| G: runbook, architecture, scaling and security docs, README | #105 | Norman |
| Jetstream drills: HTTPS, off-instance backup, restore, rollback, reboot | #106 | Norman |

Order: A → B → C → E (Norman, in sequence) while D, F, H run in parallel; then the
Jetstream drills (#106, need A, B, C, E merged); then docs (G); then `discussion.pdf`,
slides and the final `v1.0.0` release.

## Done

- **Workstream A (#98)**: commit `3ab9e3e` on branch `98-ci-release` (pushed; PR not
  opened yet).
  - `ci.yml`: tests, a gitleaks scan of the whole history, and an image job that builds
    the image, waits for its HEALTHCHECK and `/ready`, and checks
    `eval/smoke-seed.jsonl` plus the version. PRs build only; `main` pushes
    `ghcr.io/normangw/entity-resolver:<sha>` and `:main`.
  - `release.yml`: a `vX.Y.Z` tag matching `pyproject.toml` retags the tested image
    and creates a GitHub Release from `CHANGELOG.md`.
  - The version lives only in `pyproject.toml` (`APP_VERSION` removed).
  - `scripts/monitor.py --smoke FILE --expect-version V`.
  - Actions pinned by SHA, base and uv images by digest, gitleaks in pre-commit,
    `CHANGELOG.md` rewritten.
  - Locally: ruff, format, mypy, pytest (195 passed) all pass; smoke mode checked
    against a live local server.

- **Workstream B (#99), code done, not yet run on Docker**: branch `99-index-deploy`,
  cut from `98-ci-release` (local commits, not pushed). Open its PR with base
  `98-ci-release`; after #98 squash-merges, cherry-pick onto a fresh branch from `main`.
  - `compose.yml`: `alias-index` at `/data/index`, `ALIAS_INDEX_PATH` from `.env`
    (no `.env`: the seed), `image: ghcr.io/normangw/entity-resolver:${VERSION:-main}`
    plus `build:` for dev.
  - `scripts/load_index.sh` (index file or backup archive into the volume, then
    `docker compose restart api`), `scripts/deploy.sh <version> | --previous
    [--dry-run]`, deploy log `~/entity-resolver-deploy.log` with result `ok`, `WARN`
    or `failed-<step>`.
  - Deploy check rule: any wrong answer fails; a 503 on a `"source": "local"` case
    fails; a 503 on a Wikidata case is retried once after 10 s, then `WARN`.
  - `eval/smoke.jsonl`: 18 cases, every answer checked against the full index and
    live Wikidata on 2026-11-23. `tests/test_smoke_file.py` re-checks the local ones
    offline when `data/companies.full.json` is present.
  - Never run with Docker (no Docker on either machine so far): the docker steps of
    both scripts are first exercised in the Jetstream drills (#106).
  - Separate branch `99-commit-full-index` (b391d84) commits the full index to git;
    not merged into `99-index-deploy`.

## Verification status

- **Verified on GitHub** (CI run 45228277809 on `98-ci-release`, all jobs green): the
  image builds, passes its HEALTHCHECK and `/ready`, answers `eval/smoke-seed.jsonl`
  correctly and reports the `pyproject.toml` version; the gitleaks scan of the whole
  history found no secrets.
- **Not verified yet:** the publish-to-GHCR steps (they run only on `main`) and
  `release.yml` (runs only on a tag). Watch both on the first merge and the first tag.
- There is no Docker on the Mac this was written on; the image was only ever built in
  CI. actionlint was not run locally (CI parsed and ran the workflows fine).

## Open decisions (Norman)

0. Bugs found while writing `eval/smoke.jsonl` (outside B; not fixed, not in the
   smoke set): `"N/A"` resolves through Wikidata to "Tomlinson Street electrical
   substation", because `_GENERIC_NAMES` in `resolver.py` is built with `.split()`,
   so `"n a"` is never a member. `"Georgetown University"` resolves to "Georgetown
   University Press" via Wikidata. Candidates for a tester-fix PR (H, #104).

1. Open the PR for `98-ci-release` (closes #98) and get a teammate's review.
2. After the first push to `main`, make the GHCR package public (package settings →
   Change visibility); otherwise the instance needs `docker login` with a read-only
   token.
3. Tag `v0.1.0` on commit `9031f63` (CHANGELOG.md says that tag exists, and it gives
   `deploy.sh --previous` something to roll back to).

## Warning: macOS iCloud "Optimize Storage"

On the old Mac the Desktop folder is synced by iCloud with "Optimize Storage" on, so
files in `~/Desktop/DSAN/entity-resolver` were `dataless` (moved to the cloud).
`import fastapi` took 82 s and `git` reads stalled. Work from a clone outside Desktop
and iCloud (for example `~/code/entity-resolver`), or set the folder to "Keep
Downloaded" in Finder. Note that `CLAUDE.md` and `data/companies.full.json` are
git-ignored and are not in the repo; copy them over by hand.

## Next step

Finish **Workstream B (#99)**: push `99-index-deploy`, open its PR (base
`98-ci-release`), then start **Workstream C (#100)** on a branch cut from it.

Setup on a new machine:

```
git clone https://github.com/normangw/entity-resolver.git && cd entity-resolver
git switch 98-ci-release
uv sync --extra dev --frozen
uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest
```
