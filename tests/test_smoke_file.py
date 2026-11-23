"""eval/smoke.jsonl, the known-good answers scripts/deploy.sh checks after a deploy.

The file must parse, and every case it marks as answered from the local index must
still be answered that way by the app on the real full index, offline. The full index
is git-ignored, so that check runs only where data/companies.full.json exists.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_main import wait_ready

from entity_resolver.config import settings
from entity_resolver.main import app

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import monitor  # noqa: E402

SMOKE = ROOT / "eval" / "smoke.jsonl"
FULL_INDEX = ROOT / "data" / "companies.full.json"


def test_the_full_smoke_file_is_valid():
    cases = monitor.load_cases(str(SMOKE))
    assert len(cases) >= 15
    assert any(monitor.is_local(c) for c in cases)
    assert any(c.get("source") == "wikidata" for c in cases)
    assert any(c.get("status") == "unresolved" for c in cases)


@pytest.mark.skipif(not FULL_INDEX.exists(), reason="needs data/companies.full.json")
def test_local_smoke_answers_hold_on_the_full_index(monkeypatch):
    local = [c for c in monitor.load_cases(str(SMOKE)) if monitor.is_local(c)]
    monkeypatch.setattr(settings, "alias_index_path", FULL_INDEX)
    monkeypatch.setattr(settings, "rate_limit_resolve_per_min", 0)
    wrong = []
    with TestClient(app) as client:
        wait_ready(client, timeout=60)
        for case in local:
            request = {k: case[k] for k in monitor.REQUEST_KEYS if k in case}
            response = client.post("/resolve", json=request)
            assert response.status_code == 200, case
            wrong += [
                f"{case['company_name']}: {m}"
                for m in monitor.mismatches(case, response.json())
            ]
    assert wrong == []
