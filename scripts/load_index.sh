#!/usr/bin/env bash
# Load the full alias index into the `alias-index` volume, from a local index file or
# a backup archive written by scripts/backup_index.py.
#
#   scripts/load_index.sh data/companies.full.json
#   scripts/load_index.sh backups/alias-index-20261027.tar.gz
#
# Run it from the repository checkout on the server. It starts a one-off container
# from the api image with the volume mounted at /data/index, and inside it:
#   * an archive is verified (checksum, company count, index loads) and restored;
#   * an index file is first packed into an archive the same way, so it gets the
#     same checks, then restored.
# Either way the volume ends up with /data/index/companies.full.json (and its
# .meta.json), written atomically, so a failed load leaves the old index in place.
#
# The app reads that file only when .env says
#   ALIAS_INDEX_PATH=/data/index/companies.full.json
# and loads it once at startup, so a running api is restarted at the end.
# VERSION picks the image, as in compose.yml (default: main).
set -euo pipefail

usage() {
    echo "usage: $0 <companies.full.json | alias-index-*.tar.gz>" >&2
    exit 2
}

[ $# -eq 1 ] || usage
src=$1
[ -f "$src" ] || { echo "error: $src is not a file" >&2; exit 1; }

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"

src_dir=$(cd "$(dirname "$src")" && pwd)
src_name=$(basename "$src")

case "$src_name" in
    *.tar.gz)
        # shellcheck disable=SC2016  # expanded by the container's shell
        inner='python /scripts/backup_index.py restore "/in/$1" /data/index'
        ;;
    *.json)
        # backup_index.py also picks up <name>.meta.json beside the index if present.
        # shellcheck disable=SC2016
        inner='python /scripts/backup_index.py backup "/in/$1" /tmp/load \
            && python /scripts/backup_index.py restore /tmp/load/alias-index-*.tar.gz /data/index'
        ;;
    *)
        echo "error: expected a .json index or a .tar.gz backup archive" >&2
        exit 2
        ;;
esac

echo "loading $src into the alias-index volume"
# --no-deps: do not start the proxy; the scripts are bind-mounted because the image
# does not ship them; the source directory is mounted read-only.
docker compose run --rm --no-deps -T \
    -v "$repo/scripts:/scripts:ro" \
    -v "$src_dir:/in:ro" \
    --entrypoint sh \
    api -c "$inner" sh "$src_name"

if [ -n "$(docker compose ps -q api 2>/dev/null)" ]; then
    echo "restarting api to load the new index"
    docker compose restart api
fi

if ! grep -qs '^ALIAS_INDEX_PATH=/data/index/companies.full.json' .env; then
    echo "note: .env does not set ALIAS_INDEX_PATH=/data/index/companies.full.json," >&2
    echo "      so the app still serves the bundled seed index." >&2
fi
echo "done; check readiness with: curl -s http://127.0.0.1/ready"
