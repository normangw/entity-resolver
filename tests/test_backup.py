import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import backup_index  # noqa: E402


def make_index(dir_: Path) -> Path:
    index = dir_ / "companies.full.json"
    records = [
        {"wikidata_id": f"Q{i}", "canonical_name": f"Co {i}", "aliases": [f"Co {i}"]}
        for i in range(1, 4)
    ]
    index.write_text(json.dumps(records), encoding="utf-8")
    (dir_ / "companies.full.meta.json").write_text(
        json.dumps({"built_at": "2026-10-27T18:22:48+00:00"}), encoding="utf-8"
    )
    return index


def test_backup_then_restore_gives_back_the_same_bytes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    index = make_index(src)
    archive = backup_index.backup(index, tmp_path / "backups")
    assert archive.name == "alias-index-20261027.tar.gz"
    restored = backup_index.restore(archive, tmp_path / "restored")
    assert restored.read_bytes() == index.read_bytes()


def test_a_damaged_archive_is_refused_and_nothing_is_written(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    archive = backup_index.backup(make_index(src), tmp_path / "backups")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(archive) as old, tarfile.open(bad, "w:gz") as new:
        for member in old.getmembers():
            data = old.extractfile(member).read()  # type: ignore[union-attr]
            if member.name == "companies.full.json":
                data = data.replace(b"Co 1", b"Co X")
            member.size = len(data)
            import io

            new.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="checksum"):
        backup_index.restore(bad, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_a_file_that_is_not_an_index_is_not_backed_up(tmp_path):
    junk = tmp_path / "companies.full.json"
    junk.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        backup_index.backup(junk, tmp_path / "backups")
