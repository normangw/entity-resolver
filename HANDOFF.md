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

## Not verified yet

- **The CI image job has never run.** There is no Docker on the Mac this was written
  on, so the build, the HEALTHCHECK wait and the smoke test of the real image run for
  the first time on GitHub.
- **The gitleaks history scan has never run.** If it finds a secret, CI goes red:
  rotate that key, then decide whether to allowlist the finding.
- The workflows were only parsed as YAML; actionlint was not run.

## Open decisions (Norman)

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

Start **Workstream B (#99)** on a fresh branch from `main` (or from `98-ci-release`
until #98 merges):

1. Move the alias index out of the image: mount `alias-index` at `/data/index` and
   read `ALIAS_INDEX_PATH` from `.env` (dev default: the bundled seed).
2. `scripts/load_index.sh` to copy an index or a backup archive into the volume.
3. `scripts/deploy.sh <version>` and `deploy.sh --previous` (pull, record the digest,
   `up -d`, wait healthy, run `eval/smoke.jsonl`, append to the deploy log).
4. `compose.yml`: `image: ghcr.io/normangw/entity-resolver:${VERSION:-main}`, keep
   `build:` for local dev.
5. Write `eval/smoke.jsonl` (full index: Lyft, YouTube → Google, a typo, a non-English
   alias, same-name cases, gibberish, a non-company). Check every expected answer
   against the real full index before committing.

Setup on a new machine:

```
git clone https://github.com/normangw/entity-resolver.git && cd entity-resolver
git switch 98-ci-release
uv sync --extra dev --frozen
uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest
```
