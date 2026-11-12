"""Back up and restore the alias index, the one file that is slow to rebuild.

``data/companies.full.json`` takes about 20 minutes to rebuild from Wikidata and is
git-ignored, so it exists only where it was built. This writes it, its metadata and a
SHA-256 manifest into one ``.tar.gz``, and restores from that archive after checking
the checksum and that the index still loads and has the recorded number of companies.

    python scripts/backup_index.py backup data/companies.full.json backups/
    python scripts/backup_index.py verify backups/alias-index-20261027.tar.gz
    python scripts/backup_index.py restore backups/alias-index-20261027.tar.gz data/

Keep the archive somewhere other than the machine it came from (a GitHub release
asset, a Jetstream volume snapshot, a cloud bucket). See "Backup and restore" in the
README.
"""

import argparse
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

INDEX_NAME = "companies.full.json"
META_NAME = "companies.full.meta.json"
MANIFEST = "MANIFEST.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_index(data: bytes) -> int:
    """Raise unless ``data`` is an alias index; return how many companies it holds."""
    records = json.loads(data.decode("utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("the index is empty or is not a list of companies")
    for rec in records[:50]:
        if not {"wikidata_id", "canonical_name", "aliases"} <= rec.keys():
            raise ValueError("an index record is missing a required field")
    return len(records)


def backup(index: Path, out_dir: Path) -> Path:
    data = index.read_bytes()
    companies = _check_index(data)
    meta_path = index.with_name(META_NAME)
    meta = meta_path.read_bytes() if meta_path.exists() else b"{}"
    built = json.loads(meta).get("built_at", "unknown")[:10].replace("-", "")
    manifest = {
        "index_sha256": _sha256(data),
        "companies": companies,
        "built_at": built,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"alias-index-{built}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, payload in (
            (INDEX_NAME, data),
            (META_NAME, meta),
            (MANIFEST, json.dumps(manifest, indent=2).encode()),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    verify(archive)  # never leave an archive that could not be restored
    return archive


def _read(archive: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            # Only the three expected plain files; never a path from the archive.
            if member.name in (INDEX_NAME, META_NAME, MANIFEST) and member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    missing = {INDEX_NAME, META_NAME, MANIFEST} - out.keys()
    if missing:
        raise ValueError(f"archive is missing {sorted(missing)}")
    return out


def verify(archive: Path) -> dict[str, object]:
    files = _read(archive)
    manifest = json.loads(files[MANIFEST])
    if _sha256(files[INDEX_NAME]) != manifest["index_sha256"]:
        raise ValueError("checksum mismatch: the archive is damaged")
    companies = _check_index(files[INDEX_NAME])
    if companies != manifest["companies"]:
        raise ValueError("company count differs from the manifest")
    return manifest


def restore(archive: Path, dest_dir: Path) -> Path:
    verify(archive)
    files = _read(archive)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / INDEX_NAME
    for name in (INDEX_NAME, META_NAME):
        # Write beside the target and rename, so a crash never leaves half a file.
        tmp = dest_dir / f".{name}.tmp"
        tmp.write_bytes(files[name])
        tmp.replace(dest_dir / name)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="write a verified archive")
    b.add_argument("index", type=Path)
    b.add_argument("out_dir", type=Path)
    v = sub.add_parser("verify", help="check an archive without restoring it")
    v.add_argument("archive", type=Path)
    r = sub.add_parser("restore", help="verify an archive, then write the index")
    r.add_argument("archive", type=Path)
    r.add_argument("dest_dir", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            print(f"wrote {backup(args.index, args.out_dir)}")
        elif args.command == "verify":
            print(json.dumps(verify(args.archive), indent=2))
        else:
            print(f"restored {restore(args.archive, args.dest_dir)}")
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
