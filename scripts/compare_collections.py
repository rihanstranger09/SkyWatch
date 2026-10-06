#!/usr/bin/env python3
"""Compare two processed collections of the same ground (change screening).

    python scripts/compare_collections.py \
        --previous artifacts/epoch-a_ndvi_cog.tif \
        --current  artifacts/epoch-b_ndvi_cog.tif \
        --out artifacts/change-report.json

Both files are products this line wrote, so band 1 is the vegetation index and
band 2 is the water index. The script reports how much of the scene lost or
gained index value between the two epochs, and screens the result into a verdict.

Exit codes
----------
0  comparison produced (even if the verdict is NO SIGNIFICANT CHANGE)
2  inputs are not comparable (different shape/CRS) - reported, not silently resampled
3  a file could not be read
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.change import CHANGE_THRESHOLD, compare, compare_stats  # noqa: E402
from src.indices import index_stats, terrain_composition, trafficability  # noqa: E402


def read_index(path: Path, band: int = 1) -> np.ndarray:
    import rasterio

    with rasterio.open(path) as dataset:
        return dataset.read(band)


def read_index_pair(path: Path):
    """Band 1 is the vegetation index, band 2 the water index (see src/handler.py)."""
    import rasterio

    with rasterio.open(path) as dataset:
        count = dataset.count
        vegetation = dataset.read(1)
        water = dataset.read(2) if count >= 2 else None
    return vegetation, water


def read_grid(path: Path):
    import rasterio

    with rasterio.open(path) as dataset:
        return {
            "crs": str(dataset.crs),
            "shape": (dataset.height, dataset.width),
            "bounds": [round(v, 6) for v in dataset.bounds],
            "transform": list(dataset.transform)[:6],
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--previous", required=True, help="earlier collection product (COG written by this line)")
    parser.add_argument("--current", required=True, help="later collection product")
    parser.add_argument("--threshold", type=float, default=CHANGE_THRESHOLD,
                        help=f"index delta counted as change (default {CHANGE_THRESHOLD})")
    parser.add_argument("--out", help="write the report as JSON to this path")
    parser.add_argument("--quiet", action="store_true", help="only write the report, print nothing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    previous_path, current_path = Path(args.previous), Path(args.current)

    for path in (previous_path, current_path):
        if not path.exists():
            print(f"ERROR: not found: {path}", file=sys.stderr)
            return 3

    try:
        previous, _ = read_index_pair(previous_path)
        current, current_water = read_index_pair(current_path)
        previous_grid = read_grid(previous_path)
        current_grid = read_grid(current_path)
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"ERROR: could not read a product: {exc}", file=sys.stderr)
        return 3

    if previous_grid["shape"] != current_grid["shape"] or previous_grid["crs"] != current_grid["crs"]:
        print("ERROR: the two products are not comparable "
              f"(shape {previous_grid['shape']} vs {current_grid['shape']}, crs {previous_grid['crs']} vs {current_grid['crs']})",
              file=sys.stderr)
        return 2

    result = compare(previous, current, threshold=args.threshold)
    # terrain composition uses both indices, exactly as the worker computes it
    composition = terrain_composition(current, current_water)
    report = {
        "reportVersion": 1,
        "previous": {"path": str(previous_path), **previous_grid, "indexStats": index_stats(previous)},
        "current": {"path": str(current_path), **current_grid, "indexStats": index_stats(current)},
        "change": result,
        "statistics": compare_stats(index_stats(previous), index_stats(current)),
        "currentTerrain": composition,
        "currentMobility": trafficability(composition),
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not args.quiet:
        print(f"previous : {previous_path}  ({previous_grid['shape'][1]}x{previous_grid['shape'][0]} {previous_grid['crs']})")
        print(f"current  : {current_path}")
        print(f"verdict  : {result['status']}  -  {result.get('note', '')}")
        print(f"changed  : {result['changedPct']}% of scene  (loss {result['lossPct']}%  gain {result['gainPct']}%)")
        print(f"mean d   : {result['meanDelta']}")
        print(f"terrain  : {report['currentTerrain']['vegetationPct']}% vegetation · "
              f"{report['currentTerrain']['waterPct']}% water · mobility {report['currentMobility']['class']}")
        if args.out:
            print(f"report   : {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
