"""Static checks on compose.yml and the Caddyfile (#100).

They cannot prove Caddy works (that needs a Docker host, see docs/drills.md), but
they stop the published ports, pins and the /metrics block from drifting.
"""

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")  # comes with the dev tools (pre-commit)

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    """compose.yml, parsed."""
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def test_only_the_proxy_publishes_ports_and_only_80_and_443_tcp():
    """Only the proxy is reachable from outside, on 80 and 443/tcp.

    Other services may bind to host loopback only (Grafana, reached over an SSH
    tunnel), which the internet cannot reach.
    """
    services = _compose()["services"]
    published = {name: svc.get("ports", []) for name, svc in services.items()}
    assert published.pop("proxy") == ["80:80/tcp", "443:443/tcp"]
    public = {
        name: [p for p in ports if not str(p).startswith("127.0.0.1:")]
        for name, ports in published.items()
    }
    assert all(not ports for ports in public.values()), public


def test_every_image_is_pinned_by_digest():
    """Each image is ``name:tag@sha256:...``, except our own, pinned by VERSION."""
    for name, svc in _compose()["services"].items():
        image = svc["image"]
        if image.startswith("ghcr.io/normangw/entity-resolver:"):
            continue
        assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", image), name


def test_caddy_keeps_its_certificates_and_restarts():
    """Certificates survive a recreate, and the proxy comes back after a reboot."""
    compose = _compose()
    proxy = compose["services"]["proxy"]
    assert "caddy-data:/data" in proxy["volumes"]
    assert "caddy-config:/config" in proxy["volumes"]
    assert {"caddy-data", "caddy-config"} <= set(compose["volumes"])
    assert proxy["restart"] == "unless-stopped"
    assert proxy["depends_on"]["api"]["condition"] == "service_healthy"
    assert "healthcheck" in proxy


def test_caddyfile_blocks_metrics_and_turns_http3_off():
    """/metrics is a 404 from outside, and no UDP port is needed."""
    text = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert re.search(r"@metrics path /metrics", text)
    assert "respond @metrics 404" in text
    assert re.search(r"protocols h1 h2\s*\n", text)
    assert "{$SITE_ADDRESS" in text
    assert "reverse_proxy api:8000" in text


def test_nginx_is_gone():
    """Caddy replaced nginx; no config for it is left behind."""
    assert not (ROOT / "nginx.conf").exists()
