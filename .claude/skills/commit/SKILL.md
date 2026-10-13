---
name: commit
description: Commit code following the entity-resolver team workflow - create a branch, run checks, commit, push the branch, and open a PR. Use when the user asks to commit, push, submit changes, open a PR, or says a change is done and should go into the repo.
---

# Commit code (branch + PR workflow)

Goal: every commit on `main` has gone through a PR and CI, and a teammate reading
the PR can tell what changed and why. So no change is pushed straight to `main`;
everything goes branch → push branch → open PR → teammate merges.

## Workflow

### 1. Understand the changes
Run `git status` and `git diff`. If the changes mix several unrelated things, split
them into separate commits; if they are very different, split them into separate
branches and PRs so a reviewer only has to understand one thing at a time.

### 2. Make sure you are on a feature branch
- Currently on `main`: run `git fetch origin`, then `git switch -c <branch-name>`
  (uncommitted changes come along).
- Already on a feature branch: keep using it.

Name branches `<type>/<short-description>`, lowercase, hyphen-separated:

| Type | Use for | Example |
|---|---|---|
| `feat` | New functionality | `feat/wikidata-lookup` |
| `fix` | Bug fixes | `fix/health-endpoint-timeout` |
| `docs` | Documentation only | `docs/deployment-guide` |
| `test` | Tests only | `test/resolver-edge-cases` |
| `chore` | Config, dependencies, CI | `chore/bump-fastapi` |

### 3. Run the four CI checks locally
These mirror `.github/workflows/ci.yml`. Running them first avoids finding out
about a red build only after pushing:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run pytest
```

Stop if any of them fails. Formatting issues can be fixed with
`uv run ruff check --fix .` and `uv run ruff format .`; type errors and test
failures must be fixed or reported to the user first. Do not commit with failures.

### 4. Stage file by file
Use `git add <specific files>`, not `git add -A` or `git add .`, so unrelated files
do not slip in. Then review `git diff --staged` and confirm:

- If dependencies in `pyproject.toml` changed, `uv.lock` must be committed with it
  (CI runs `uv sync --frozen` and fails when the two are out of sync).
- No `.env*` files are committed other than `.env.example` and `.env.production`,
  and those two contain no keys, tokens, or passwords. `.gitignore` only ignores
  `.env`, so a file like `.env.local` is not blocked automatically and has to be
  caught here.
- No datasets, model weights, notebook outputs, or other large files.

### 5. Write the commit message
Follow the existing style in this repo:

- English, imperative mood, capitalized, no trailing period, subject line at most
  72 characters
- Say what the commit does, e.g. `Add Wikidata lookup client with timeout handling`
- When the reason is not obvious, add a blank line and a body explaining why

The pre-commit hook may modify files and abort the commit. If so, `git add` the
modified files again and commit once more.

### 6. Sync with main
After `git fetch origin`:

- Branch has not been pushed yet: `git rebase origin/main` for a cleaner history.
- Branch has already been pushed (especially with an open PR): `git merge origin/main`
  to avoid rewriting history others may have pulled.

On conflicts, stop and list the conflicting files for the user. Do not guess at a
resolution. If the sync brought in new code, rerun the checks from step 3.

### 7. Push the branch
```bash
git push -u origin <branch-name>
```
If the push is rejected for lack of permission, the current account is not a
collaborator on the repo. Tell the user and stop.

### 8. Open the PR
The base branch is `main`. Prefer the GitHub CLI:

```bash
gh pr create --base main --title "<title>" --body "<body>"
```

If `gh` is not installed, give the user the PR creation link from the push output.

- **Title**: same style as a commit message, summarizing the whole PR.
- **Body** uses this structure:

```markdown
## What
What this PR changes (two or three sentences).

## Why
Why the change is needed; link the related assignment or issue.

## Testing
Which checks were run locally and their results; which tests were added.
```

If the branch already has an open PR, do not create another one. Pushing updates
the existing PR.

### 9. Report back
Tell the user: branch name, commit hash and message, files included, PR link, and
current CI status (`gh pr checks` shows it).

## Do not
- Commit or push directly to `main`
- Merge the PR yourself; merging is decided by teammates after review
- Use `--no-verify` to skip pre-commit
- Use `git push --force`; if a pushed branch really needs rewriting, explain why,
  get the user's agreement, and then use `--force-with-lease`
- Commit "for now" when checks have not passed

## After the PR is merged
When the user says the PR has been merged, help clean up:

```bash
git switch main
git pull origin main
git branch -d <branch-name>
```
