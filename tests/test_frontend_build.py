"""Tests for the dashboard build tooling (`frontend/publish_status.py`, `embed_assets.py`).

The dashboard is the only user-facing artifact of this repository, so its build
steps are covered like any other code: the status injection must be surgical
(replacing a marked region, never mangling the page), the publisher must degrade
to the committed fallback, and the embedded asset bundle must stay in sync with
`frontend/assets/`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publisher = load_module("publish_status", FRONTEND / "publish_status.py")
embedder = load_module("embed_assets", FRONTEND / "embed_assets.py")


# --------------------------------------------------------------------------- #
# status injection
# --------------------------------------------------------------------------- #
def test_inject_status_replaces_only_the_marked_region():
    html = "<html>keep-me" + publisher.START + "old payload" + publisher.END + "tail</html>"
    status = {"result": {"status": "SUCCEEDED", "ndvi": {"mean": 0.42}}}

    out = publisher.inject_status(html, status)

    assert out.startswith("<html>keep-me")
    assert out.endswith("tail</html>")
    assert "old payload" not in out
    payload = out.split(publisher.START, 1)[1].split(publisher.END, 1)[0]
    assert "window.__PIPELINE_STATUS__ = {" in payload
    # the injected JSON payload must round-trip
    encoded = payload.split("window.__PIPELINE_STATUS__ = ", 1)[1].rstrip().rstrip(";")
    assert json.loads(encoded) == status


def test_inject_status_is_a_noop_without_markers():
    out = publisher.inject_status("<html>nothing here</html>", {"a": 1})
    assert out == "<html>nothing here</html>"


def test_inject_status_leaves_page_untouched_when_status_missing():
    html = "<html>" + publisher.START + "keep" + publisher.END + "</html>"
    assert publisher.inject_status(html, None) == html


# --------------------------------------------------------------------------- #
# publisher end to end
# --------------------------------------------------------------------------- #
def test_publisher_writes_a_self_contained_artifact(tmp_path, monkeypatch, capsys):
    live = tmp_path / "pipeline-status.json"
    live.write_text(json.dumps({"source": "github-actions", "result": {"status": "SUCCEEDED"}}), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "publish_status.py",
        "--site-dir", str(tmp_path / "_site"),
        "--status", str(live),
        "--fallback", str(FRONTEND / "pipeline-status.json"),
    ])
    assert publisher.main() == 0

    artifact = tmp_path / "_site" / "index.html"
    text = artifact.read_text(encoding="utf-8")
    assert '<canvas' in text and "<svg" in text
    assert not re.search(r"<script[^>]+src\s*=\s*['\"]https?://", text, re.I)
    assert '"source":"github-actions"' in text.replace(" ", "")
    assert (tmp_path / "_site" / "pipeline-status.json").exists()
    assert (tmp_path / "_site" / "assets" / "ndvi-matrix.b64").exists()
    capsys.readouterr()


def test_publisher_falls_back_to_the_committed_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "publish_status.py",
        "--site-dir", str(tmp_path / "_site"),
        "--status", str(tmp_path / "does-not-exist.json"),
        "--fallback", str(FRONTEND / "pipeline-status.json"),
    ])
    assert publisher.main() == 0

    payload = json.loads((tmp_path / "_site" / "pipeline-status.json").read_text(encoding="utf-8"))
    assert payload["result"]["status"] == "SUCCEEDED"
    assert "live snapshot unavailable" in capsys.readouterr().out


def test_publisher_check_mode_reports_success():
    args = ["publish_status.py", "--check", "--status", str(FRONTEND / "pipeline-status.json")]
    old = sys.argv
    try:
        sys.argv = args
        assert publisher.main() == 0
    finally:
        sys.argv = old


# --------------------------------------------------------------------------- #
# embedded assets stay in sync
# --------------------------------------------------------------------------- #
def test_embedded_assets_are_present_and_valid():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    block = html.split(embedder.START, 1)[1].split(embedder.END, 1)[0]
    assert "data:image/png;base64," in block, "hero/RGB previews must be embedded as data URIs"

    for key in ("assets/ndvi-matrix.b64", "assets/ndwi-matrix.b64", "assets/scene-meta.json"):
        assert f'"{key}"' in block, f"{key} is not embedded"

    # the embedded NDVI matrix must decode to a square float32 grid
    import base64
    import struct

    match = re.search(r'"assets/ndvi-matrix\.b64":\s*"([^"]+)"', block)
    assert match, "embedded NDVI matrix not found"
    raw = base64.b64decode(match.group(1))
    assert len(raw) % 4 == 0
    size = int((len(raw) // 4) ** 0.5)
    assert size * size * 4 == len(raw), "matrix is not square"
    values = struct.unpack(f"<{size * size}f", raw)
    assert all(-1.001 <= v <= 1.001 for v in values[:1000]), "NDVI values must stay in [-1, 1]"


def test_embedder_check_mode_is_current():
    """Guards against editing assets without re-running embed_assets.py."""
    old = sys.argv
    try:
        sys.argv = ["embed_assets.py", "--check"]
        assert embedder.main() == 0
    finally:
        sys.argv = old


def test_fallback_snapshot_has_the_dashboard_contract():
    status = json.loads((FRONTEND / "pipeline-status.json").read_text(encoding="utf-8"))
    for key in ("source", "generatedAt", "stack", "region", "image", "result", "stages", "checks"):
        assert key in status, f"missing key {key}"
    assert len(status["stages"]) == 6
    assert status["result"]["status"] in {"SUCCEEDED", "FAILED"}
    ndvi = status["result"]["ndvi"]
    assert -1.0 <= ndvi["min"] <= ndvi["mean"] <= ndvi["max"] <= 1.0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
