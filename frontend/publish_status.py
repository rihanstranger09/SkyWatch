#!/usr/bin/env python3
"""Build the GitHub Pages artefact for the pipeline dashboard.

``frontend/index.html`` is a zero-dependency, fully self-contained page. This
script produces ``_site/`` by:

1. copying the dashboard across,
2. replacing the region between ``__PIPELINE_STATUS_START__`` /
   ``__PIPELINE_STATUS_END__`` with the latest run snapshot (so the published
   page renders that exact run without a second network request),
3. shipping the snapshot as a sibling ``pipeline-status.json`` as well, which
   keeps the raw data available for scripts and for the CI artifact.

It never fails the workflow on a missing snapshot: if the integration test did
not run, the committed fallback (``frontend/pipeline-status.json``) is used.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "/* __PIPELINE_STATUS_START__ */"
END = "/* __PIPELINE_STATUS_END__ */"
EXTRAS = ("assets", "img", "favicon.ico", "robots.txt")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", default="_site", help="output directory for the Pages artefact")
    parser.add_argument("--status", default="pipeline-status.json", help="snapshot produced by the e2e test")
    parser.add_argument("--fallback", default="frontend/pipeline-status.json", help="committed fallback snapshot")
    parser.add_argument("--check", action="store_true", help="verify the output without writing it")
    return parser.parse_args()


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        print(f"WARNING: {path} is not valid JSON ({exc}); ignoring it", file=sys.stderr)
        return None


def inject_status(html: str, status) -> str:
    """Replace the marked status region with the given snapshot."""
    if START not in html or END not in html:
        print(f"WARNING: status markers not found; leaving {START} region untouched", file=sys.stderr)
        return html
    payload = json.dumps(status or {}, separators=(",", ":")) if status is not None else None
    if payload is None:
        return html
    head, rest = html.split(START, 1)
    _, tail = rest.split(END, 1)
    block = (
        f"{START}\n"
        "window.__PIPELINE_STATUS__ = "
        f"{payload};\n"
        f"{END}"
    )
    return head + block + tail


def main() -> int:
    args = parse_args()
    source = ROOT / "frontend" / "index.html"
    if not source.exists():
        print(f"ERROR: {source} not found - nothing to publish", file=sys.stderr)
        return 1

    status = load_json(ROOT / args.status)
    origin = args.status
    if status is None:
        status = load_json(ROOT / args.fallback)
        origin = args.fallback
        print(f"INFO: live snapshot unavailable ({args.status}); using {args.fallback}")

    html = inject_status(source.read_text(encoding="utf-8"), status)

    if args.check:
        ok = status is not None and "window.__PIPELINE_STATUS__ = {" in html
        print(f"{'OK' if ok else 'FAIL'}: publisher dry run (snapshot from {origin})")
        return 0 if ok else 1

    site = (ROOT / args.site_dir).resolve()
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(html, encoding="utf-8")
    if status is not None:
        (site / "pipeline-status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    for extra in EXTRAS:
        candidate = ROOT / "frontend" / extra
        if not candidate.exists():
            continue
        destination = site / extra
        if candidate.is_dir():
            shutil.copytree(candidate, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(candidate, destination)

    if status:
        result = status.get("result", {}) or {}
        ndvi = result.get("ndvi") or {}
        print(
            "INFO: publishing run {run} -> status={status} ndvi_mean={ndvi} source={source}".format(
                run=(status.get("github") or {}).get("runNumber", "local"),
                status=result.get("status", "unknown"),
                ndvi=ndvi.get("mean"),
                source=status.get("source", origin),
            )
        )
    print(f"SUCCESS: Pages artefact ready in {site}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
