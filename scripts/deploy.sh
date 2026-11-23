#!/usr/bin/env bash
# Deploy a published image, check it, and record the result. Rolling back is the same
# command with an older version.
#
#   scripts/deploy.sh 0.2.0          # a release (v0.2.0 also works)
#   scripts/deploy.sh 3ab9e3e...     # any commit CI published (full sha), or main
#   scripts/deploy.sh --previous     # roll back to the last good version before this one
#   scripts/deploy.sh 0.2.0 --dry-run   # show what would run; change nothing
#
# Steps: pull ghcr.io/normangw/entity-resolver:<version>, record its digest, start it
# with `docker compose up -d`, wait until the container is healthy and /ready answers,
# then check the known-good answers in eval/smoke.jsonl with scripts/monitor.py (and,
# for a release, that /health reports that version). Every attempt appends one line
#   <UTC timestamp> <version> <image digest> <result>
# to the deploy log, where result is ok or failed-<step>. On a failure it exits 1 and
# prints the rollback command.
#
# --previous reads the log: the version to go back to is the newest "ok" line whose
# version differs from the last line's (so it also works right after a failed deploy).
#
# Run it from the repository checkout on the server, with the full index loaded (see
# scripts/load_index.sh). Always deploy through this script: a bare `docker compose
# up -d` uses VERSION from the shell, or main when it is unset.
#
# Settings (environment variables):
#   DEPLOY_LOG      deploy log          (default ~/entity-resolver-deploy.log)
#   DEPLOY_URL      address to check    (default http://127.0.0.1, through the proxy)
#   DEPLOY_SMOKE    known-good answers  (default eval/smoke.jsonl)
#   DEPLOY_TIMEOUT  seconds to wait for healthy and ready (default 180)
set -euo pipefail

IMAGE=ghcr.io/normangw/entity-resolver
LOG=${DEPLOY_LOG:-$HOME/entity-resolver-deploy.log}
URL=${DEPLOY_URL:-http://127.0.0.1}
SMOKE=${DEPLOY_SMOKE:-eval/smoke.jsonl}
TIMEOUT=${DEPLOY_TIMEOUT:-180}

usage() {
    echo "usage: $0 <version> | --previous  [--dry-run]" >&2
    exit 2
}

version=""
previous=false
dry_run=false
for arg in "$@"; do
    case "$arg" in
        --previous) previous=true ;;
        --dry-run) dry_run=true ;;
        -h | --help) usage ;;
        -*) echo "error: unknown option $arg" >&2; usage ;;
        *) [ -z "$version" ] || usage; version=$arg ;;
    esac
done

if $previous; then
    [ -z "$version" ] || usage
    [ -s "$LOG" ] || { echo "error: no deploy log at $LOG" >&2; exit 1; }
    version=$(awk '
        { v[NR] = $2; r[NR] = $4 }
        END {
            for (i = NR - 1; i >= 1; i--)
                if (r[i] == "ok" && v[i] != v[NR]) { print v[i]; exit }
        }' "$LOG")
    [ -n "$version" ] || {
        echo "error: $LOG has no earlier good version to roll back to" >&2
        exit 1
    }
    echo "rolling back to $version (from $LOG)"
fi
[ -n "$version" ] || usage
version=${version#v}  # release images are tagged 0.2.0, git tags v0.2.0
case "$version" in
    *[!A-Za-z0-9._-]* | "") echo "error: bad version '$version'" >&2; exit 2 ;;
esac

monitor_args=(--smoke "$SMOKE" --p50-ms 5000 --p95-ms 15000)
if [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    monitor_args+=(--expect-version "$version")
fi

if $dry_run; then
    echo "would deploy $IMAGE:$version"
    echo "  VERSION=$version docker compose pull"
    echo "  VERSION=$version docker compose up -d --no-build"
    echo "  wait up to ${TIMEOUT}s for healthy and $URL/ready"
    echo "  python3 scripts/monitor.py $URL ${monitor_args[*]}"
    echo "  append the result to $LOG"
    exit 0
fi

cd "$(dirname "$0")/.."
export VERSION=$version
digest=-

record() {
    printf '%s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$version" "$digest" "$1" >>"$LOG"
}

fail() {
    record "failed-$1"
    echo "DEPLOY FAILED at step '$1' for version $version" >&2
    if [ "$1" != pull ]; then
        echo "roll back with: scripts/deploy.sh --previous" >&2
    fi
    exit 1
}

echo "== pull $IMAGE:$version"
docker compose pull || fail pull
digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE:$version") || fail pull
echo "digest $digest"

echo "== start"
docker compose up -d --no-build || fail start

echo "== wait for healthy and ready (up to ${TIMEOUT}s)"
deadline=$((SECONDS + TIMEOUT))
until container=$(docker compose ps -q api) && [ -n "$container" ] &&
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$container")" = healthy ] &&
    curl -fsS -o /dev/null --max-time 5 "$URL/ready"; do
    if ((SECONDS >= deadline)); then
        docker compose ps >&2
        docker compose logs --tail 30 api >&2
        fail health
    fi
    sleep 5
done

echo "== known-good answers ($SMOKE)"
python3 scripts/monitor.py "$URL" "${monitor_args[@]}" || fail smoke

record ok
echo "deployed $version ($digest); logged to $LOG"
