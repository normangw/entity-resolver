# Postmortem: a fresh install could not load the index

Blameless write-up of the first install of release 0.2.0 on a new Jetstream2
instance, 2026-11-27. Nothing reached users: the old instance kept serving until the
new one passed its checks.

## Summary

We moved the service to a new instance because the old one's 20 GB root disk was 95%
full. On the new instance, `scripts/load_index.sh` failed with `Permission denied`,
so the full index could not be restored and the api would not start. The cause was
one missing directory in the Dockerfile. It was fixed the same afternoon (#126), and
CI now checks for it.

## What happened

| Time (UTC, approximate) | Event |
|---|---|
| 20:30 | Old instance: `df -h /` shows 95% used; pruning Docker frees 1.2 GB, leaving 1.9 GB |
| 20:45 | New instance created with a 60 GB volume-backed root disk; repository cloned, stack started |
| 20:50 | The api restarts in a loop: `.env` points `ALIAS_INDEX_PATH` at the full index, which is not loaded yet (by design, see below) |
| 20:55 | `scripts/load_index.sh backups/alias-index-20261027.tar.gz` fails: `Permission denied: '/data/index/.companies.full.json.tmp'` |
| 20:57 | `docker compose run --user root ... chown` also fails: `Operation not permitted` |
| 21:00 | Plain `docker run --user root ... chown -R 1000:1000 /data/index` works; the load succeeds; Lyft and YouTube resolve |
| 21:05 | #126 merged: the image creates `/data/index`, and CI tests that the app user can write to a fresh volume |

## Why

- **A new named volume takes its owner from the image.** When Docker creates an empty
  named volume, it copies the mount point's owner and contents from the image. The
  Dockerfile created `/data/uploads`, owned by the `app` user, but not `/data/index`.
  So the `alias-index` volume, mounted at `/data/index`, was created owned by root,
  and the non-root `app` user could not write the index into it.
- **It never showed up before.** #110 moved the index from `src/` to
  `/data/index`. The old instance's volume, and every CI run, either already existed
  or never wrote to that path. Only a brand-new volume on a brand-new host hits it.
- **The first workaround failed for a good reason.** `compose.yml` drops all Linux
  capabilities (`cap_drop: ALL`), including `CHOWN`, so even root inside a
  `docker compose run` container cannot change ownership. That is the hardening
  working; the fix belongs in the image.

## How we found out

Running the fresh-install section of the runbook on a clean machine, which the
rubric asks for. The error named the exact file, so diagnosis took minutes.

## What went well

- The api refused to start without its index instead of quietly serving the
  5-company seed. A half-working production service would have been worse.
- The index backup already lived off the instance (on a laptop), so the new instance
  never depended on the old one. This doubled as the restore drill.
- The old instance kept serving until the new one passed every check.

## What went badly

- The fresh-install path had never been run end to end after #110 changed where the
  index lives. CI tested the image, not a first start with an empty volume.
- The disk filled up without warning: nothing watched free space on the instance.

## What we changed

| Action | Where | Status |
|---|---|---|
| Create `/data/index`, owned by `app`, in the image | `Dockerfile` (#126) | Done |
| CI mounts fresh volumes and checks that the app user can write both data paths | `.github/workflows/ci.yml` (#126) | Done |
| One-time `chown` for volumes created before #126, plus a troubleshooting row | `docs/runbook.md` | Done |
| Use a 60 GB root disk for new instances | `docs/runbook.md`, section 1 | Done |
| Alert on low disk space | `scripts/monitor.py` or a Prometheus node exporter | Open |
