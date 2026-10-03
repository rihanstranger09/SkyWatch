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
    # the published page is the SkyWatch console: brand, live background, map host
    assert "SkyWatch" in text
    assert '@keyframes bdrift' in text, "the living background must ship with the page"
    assert 'class="balloon' in text and 'class="bglayer' in text
    assert 'id="world"' in text and 'id="flow"' in text
    assert "skywatch:mean_ndvi" in text, "the console must describe this pipeline's catalog fields"
    external = re.findall(r"<script[^>]+src\s*=\s*['\"]([^'\"]+)['\"][^>]*>", text, re.I)
    assert external == ["https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"], external
    assert 'onerror="window.__noLeaflet=1"' in text, "the CDN map library must degrade gracefully"
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
    """The result overlay is painted from inlined bytes - verify they decode."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    block = html.split(embedder.START, 1)[1].split(embedder.END, 1)[0]
    assert "data:image/png;base64," in block, "the NDVI render must be embedded as a data URI"

    for key in ("assets/ndvi-preview.png", "assets/scene-meta.json"):
        assert f'"{key}"' in block, f"{key} is not embedded"

    # the inlined NDVI render must be a real PNG, not a placeholder
    import base64

    match = re.search(r'"assets/ndvi-preview\.png":\s*"data:image/png;base64,([A-Za-z0-9+/=]+)"', block)
    assert match, "embedded NDVI render not found"
    raw = base64.b64decode(match.group(1))
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "embedded bytes are not a PNG"
    assert len(raw) > 10_000, "embedded render looks truncated"

    # ... and the scene statistics shipped alongside it match the asset on disk
    stats = json.loads((FRONTEND / "assets" / "scene-meta.json").read_text(encoding="utf-8"))
    assert stats["crs"] == "EPSG:4326"
    ndvi = stats["ndvi"]
    assert -1.0 <= ndvi["min"] <= ndvi["mean"] <= ndvi["max"] <= 1.0
    assert str(round(ndvi["mean"], 4))[:6] in block or "ndvi" in block.lower()


def test_page_ships_an_actually_animated_background():
    """Regression guard for the requirement that the backgrounds keep moving."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    for layer in (".bglayer.back", ".bglayer.mid", ".bglayer.front"):
        assert layer in html, f"missing depth layer {layer}"
    # autonomous drift, staggered per depth so nothing moves in lockstep
    assert re.search(r"\.bglayer\.back\s+\.balloon\s*\{[^}]*--dur", html), "per-depth balloon durations missing"
    assert re.search(r"\.bglayer\.mid\s+\.balloon\s*\{[^}]*--dur", html)
    assert re.search(r"\.bglayer\.front\s+\.balloon\s*\{[^}]*--dur", html)
    assert re.search(r"animation:\s*bdrift\s+var\(--dur", html), "balloons are not driven by the drift keyframes"
    for keyframes in ("bdrift", "bgloss", "cband", "gdrift", "bpan", "skyTurn", "moteRise"):
        assert f"@keyframes {keyframes}" in html, f"missing @keyframes {keyframes}"
    # the veil turns, the layers pan, and the motes rise
    assert '.skyveil' in html and 'id="motes"' in html
    assert re.search(r"\.skyveil\{[^}]*animation:\s*skyTurn", html), "the veil must turn"
    assert re.search(r"\.bglayer\{[^}]*animation:\s*bpan", html), "the depth layers must pan"
    assert re.search(r"@keyframes bpan\{[^}]*var\(--px", html), "the pan must compose with the parallax variables"
    assert html.count('class="cloudband"') >= 3, "cloud bands must slide in more than one place"
    assert re.search(r"#motes i\{[^}]*animation:\s*moteRise", html)
    assert re.search(r"\.bglayer \.balloon\{[^}]*--dx:7vw", html), "balloon travel must be visible"
    assert re.search(r"\.bglayer\.front \.balloon\{[^}]*--dx:9vw", html), "the front layer must travel furthest"
    assert re.search(r"animation:\s*cband", html), "cloud bands must slide"
    assert re.search(r"animation:\s*gdrift", html), "in-sheet glows must float"
    assert 'class="cloudband' in html, "cloud bands must exist in the markup"
    assert re.search(r"\.glowblob\{[^}]*animation:\s*gdrift", html), "in-sheet glows must animate"
    # pointer + scroll parallax keeps the layers responsive to the user
    assert "pointermove" in html and "atmosphere()" in html
    assert "style.setProperty('--px'" in html and "style.setProperty('--py'" in html
    # ... and the whole thing steps aside for visitors who ask for less motion
    assert "prefers-reduced-motion" in html


def test_page_uses_no_external_runtime_dependencies():
    """Google Fonts and Leaflet are progressive enhancements only.

    The page must render completely from its own bytes; the only permitted
    network fetch is the optional Leaflet loader, which falls back to the
    built-in SVG map engine when the sandbox has no network.
    """
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]+src\s*=\s*['\"]([^'\"]+)['\"]", html, re.I)
    assert all("leaflet" in url for url in scripts), f"unexpected runtime dependency: {scripts}"
    # the only external stylesheet is the webfont request from the reference design;
    # it degrades to the system font stack when it cannot load
    sheets = re.findall(r"<link[^>]+rel=['\"]stylesheet['\"][^>]+href=['\"]([^'\"]+)['\"]", html, re.I)
    allowed_prefixes = ("https://fonts.googleapis.com/", "https://unpkg.com/leaflet@1.9.4/")
    assert all(url.startswith(allowed_prefixes) for url in sheets), sheets
    assert "system-ui" in html, "a system font stack must back the webfonts up"
    assert html.count("<style>") >= 1 and html.count("<script>") >= 2, "every style/behaviour must be inline"
    assert "<style>" in html and "<script>" in html
    assert "__noLeaflet" in html and "initSvgMap" in html, "the map must degrade to the offline SVG engine"


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
