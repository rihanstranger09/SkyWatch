#!/usr/bin/env python3
"""Inline the dashboard's data assets into ``frontend/index.html``.

The console is designed to work with **zero network access**: opened straight
from disk, served from GitHub Pages, or rendered inside a sandboxed preview
iframe. Asset URLs are therefore *embedded* as data URIs / base64 strings, with
the relative-file fetch kept as a graceful fallback.

Run this after regenerating assets with ``make_ndvi_preview.py``:

    python frontend/make_ndvi_preview.py
    python frontend/embed_assets.py

It rewrites only the region between the ``__EMBEDDED_ASSETS_START__`` and
``__EMBEDDED_ASSETS_END__`` markers, so the file stays valid and diff-friendly.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "frontend" / "index.html"
ASSETS = ROOT / "frontend" / "assets"

START = "/* __EMBEDDED_ASSETS_START__ */"
END = "/* __EMBEDDED_ASSETS_END__ */"
STATUS_START = "/* __PIPELINE_STATUS_START__ */"
STATUS_END = "/* __PIPELINE_STATUS_END__ */"
FALLBACK_STATUS = ROOT / "frontend" / "pipeline-status.json"

#: assets to inline, in payload order, with their data-URI mime type (raw base64 when None)
BUNDLE = (
    ("assets/ndvi-preview.png", "image/png", "pipeline NDVI render (result explorer overlay)"),
    ("assets/scene-meta.json", None, "scene statistics used by the inspector"),
)


def build_block() -> str:
    entries = []
    for relative, mime, label in BUNDLE:
        path = ROOT / "frontend" / relative
        if not path.exists():
            raise SystemExit(f"missing asset {path} — run frontend/make_ndvi_preview.py first")
        if mime is None:
            value = path.read_text(encoding="ascii").strip()          # already base64 text
        else:
            value = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        entries.append(f"  // {label} · {path.stat().st_size / 1024:.1f} KiB\n  {json.dumps(relative)}: {json.dumps(value)}")
    body = ",\n".join(entries)
    return (
        f"{START}\n"
        "window.__EMBEDDED_ASSETS__ = {\n"
        f"{body}\n"
        "};\n"
        f"{END}"
    )


def build_status_block() -> str:
    """Embed the committed fallback snapshot so the page renders without any fetch."""
    status = json.loads(FALLBACK_STATUS.read_text(encoding="utf-8"))
    payload = json.dumps(status, separators=(",", ":"))
    return f"{STATUS_START}\nwindow.__PIPELINE_STATUS__ = {payload};\n{STATUS_END}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the embedded bundle is current (CI mode)")
    args = parser.parse_args()

    html = INDEX.read_text(encoding="utf-8")
    if START not in html or END not in html:
        print(f"ERROR: markers {START} / {END} not found in {INDEX}", file=sys.stderr)
        return 1

    block = build_block()
    before, rest = html.split(START, 1)
    _, after = rest.split(END, 1)
    updated = before + block + after

    status_block = build_status_block()
    if STATUS_START in updated and STATUS_END in updated:
        head, tail = updated.split(STATUS_START, 1)
        _, tail = tail.split(STATUS_END, 1)
        updated = head + status_block + tail

    if args.check:
        stale = updated != html
        print(f"{'STALE' if stale else 'current'}: embedded assets in frontend/index.html")
        if stale:
            print("run: python frontend/embed_assets.py", file=sys.stderr)
        return 1 if stale else 0

    INDEX.write_text(updated, encoding="utf-8")
    print(f"embedded {len(BUNDLE)} assets + the fallback run snapshot into frontend/index.html")
    print(f"  index.html is now {INDEX.stat().st_size / 1024:.1f} KiB (self-contained)")
    for relative, _, label in BUNDLE:
        print(f"  + {relative:26s} {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
