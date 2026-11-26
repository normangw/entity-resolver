"""The parts of scripts/deploy.sh and scripts/load_index.sh that need no Docker.

Which version gets deployed (including ``--previous`` read from the deploy log) and
the argument checks run with ``--dry-run`` or fail before any docker command, so they
can run in CI. The docker steps themselves are exercised by the Jetstream drills.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def run(script: str, *args: str, log: Path | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEPLOY_")}
    if log is not None:
        env["DEPLOY_LOG"] = log.as_posix()
    return subprocess.run(
        [str(BASH), (SCRIPTS / script).as_posix(), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def write_log(path: Path, *lines: str) -> Path:
    path.write_text(
        "".join(
            f"2026-11-2{i}T10:00:00Z {line} ghcr.io/x@sha256:{i}\n"
            for i, line in enumerate(lines)
        )
    )
    # The log's columns are timestamp, version, digest, result; move result last.
    rows = [r.split() for r in path.read_text().splitlines()]
    path.write_text("".join(f"{r[0]} {r[1]} {r[3]} {r[2]}\n" for r in rows))
    return path


def test_a_release_is_deployed_by_version_and_checked_for_it():
    result = run("deploy.sh", "v0.2.0", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "would deploy ghcr.io/normangw/entity-resolver:0.2.0" in result.stdout
    assert "--expect-version 0.2.0" in result.stdout
    assert "--smoke eval/smoke.jsonl" in result.stdout
    assert "--warn-exit-code 3" in result.stdout


def test_a_branch_image_is_not_checked_for_a_version():
    result = run("deploy.sh", "main", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert ":main" in result.stdout
    assert "--expect-version" not in result.stdout


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        # After a good deploy, go back to the one before it.
        (["0.1.0 ok", "0.2.0 ok"], "0.1.0"),
        # After a failed deploy, go back to the last good one.
        (["0.1.0 ok", "0.2.0 ok", "0.3.0 failed-smoke"], "0.2.0"),
        # A failed attempt is never a rollback target.
        (["0.1.0 ok", "0.2.0 failed-health", "0.3.0 ok"], "0.1.0"),
        # Rolling back twice returns to where it started, like `cd -`.
        (["0.1.0 ok", "0.2.0 ok", "0.1.0 ok"], "0.2.0"),
        # A deploy that passed with Wikidata busy (WARN) is a good version.
        (["0.1.0 WARN", "0.2.0 ok"], "0.1.0"),
    ],
)
def test_previous_reads_the_last_good_version_from_the_log(tmp_path, history, expected):
    log = write_log(tmp_path / "deploy.log", *history)
    result = run("deploy.sh", "--previous", "--dry-run", log=log)
    assert result.returncode == 0, result.stderr
    assert f"rolling back to {expected}" in result.stdout
    assert f"entity-resolver:{expected}" in result.stdout


def test_previous_fails_without_an_earlier_good_version(tmp_path):
    log = write_log(tmp_path / "deploy.log", "0.1.0 ok")
    result = run("deploy.sh", "--previous", "--dry-run", log=log)
    assert result.returncode == 1
    assert "no earlier good version" in result.stderr


def test_previous_fails_without_a_log(tmp_path):
    result = run("deploy.sh", "--previous", "--dry-run", log=tmp_path / "none.log")
    assert result.returncode == 1
    assert "no deploy log" in result.stderr


@pytest.mark.parametrize(
    "args",
    [(), ("0.2.0", "0.3.0"), ("0.2.0", "--previous"), ("a;b",), ("--force",)],
)
def test_deploy_rejects_bad_arguments(args):
    assert run("deploy.sh", *args, "--dry-run").returncode == 2


def test_load_index_rejects_a_missing_file(tmp_path):
    result = run("load_index.sh", (tmp_path / "missing.json").as_posix())
    assert result.returncode == 1
    assert "is not a file" in result.stderr


def test_load_index_rejects_an_unknown_file_type(tmp_path):
    other = tmp_path / "index.csv"
    other.write_text("x")
    result = run("load_index.sh", other.as_posix())
    assert result.returncode == 2
    assert "expected a .json index" in result.stderr
