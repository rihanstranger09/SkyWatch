#!/usr/bin/env python3
"""Generate two comparable epochs of the sample tile and screen them for change.

    make change-demo          # writes artifacts/epoch-*.tif + artifacts/change-report.json

Both epochs are rendered from the same ground-truth field with the line's own
index maths (``src/indices.py``), so the demo needs no downloads and no cloud
access. The second epoch removes canopy from the northern strip and lets water
into the south, which the comparison should report as loss plus a mobility change.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.indices import compute_ndvi, compute_ndwi, terrain_composition, trafficability  # noqa: E402

SHAPE = (256, 256)
ORIGIN = (77.59, 12.97)  # Bengaluru, sample tile origin
PIXEL = 0.0001


def render(vegetation: np.ndarray):
    """Turn a vegetation field into (red, green, nir) reflectance bands."""
    red = np.clip(0.28 * (1 - vegetation) + 0.02, 0.01, 0.9)
    nir = np.clip(0.12 + 0.80 * vegetation, 0.02, 0.98)
    green = np.clip(0.20 * (1 - vegetation) + 0.03, 0.01, 0.9)
    return red.astype("float32"), green.astype("float32"), nir.astype("float32")


def write_product(path: Path, vegetation: np.ndarray, cloud_shadow: bool = False) -> dict:
    red, green, nir = render(vegetation)
    if cloud_shadow:  # an unwritten north-west corner, to prove masks are honoured
        red[:24, :24] = np.nan
        green[:24, :24] = np.nan
        nir[:24, :24] = np.nan
    ndvi = compute_ndvi(red, nir)
    ndwi = compute_ndwi(green, nir)
    profile = dict(
        driver="GTiff", tiled=True, blockxsize=128, blockysize=128, compress="deflate",
        dtype="float32", count=2, width=SHAPE[1], height=SHAPE[0], crs="EPSG:4326",
        transform=from_origin(ORIGIN[0], ORIGIN[1], PIXEL, PIXEL), nodata=np.nan,
    )
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(ndvi, 1)
        dst.write(ndwi, 2)
    composition = terrain_composition(ndvi, ndwi)
    return {"path": str(path), "composition": composition,
            "mobility": trafficability(composition)["class"]}


def main() -> int:
    outdir = ROOT / "artifacts"
    outdir.mkdir(exist_ok=True)

    rng = np.random.default_rng(7)
    rows, cols = SHAPE
    ground = np.clip(np.linspace(0.10, 0.85, cols)[None, :] + rng.normal(0, 0.05, SHAPE), 0, 1)
    later = ground.copy()
    later[:90, :] = np.clip(later[:90, :] - 0.45, 0, 1)      # canopy removed in the north
    wet = np.zeros(SHAPE, "float32")
    wet[190:, :] = 0.85                                       # water along the south edge

    earlier = write_product(outdir / "epoch-a_ndvi_cog.tif", ground.astype("float32"), cloud_shadow=True)
    current = write_product(outdir / "epoch-b_ndvi_cog.tif", later.astype("float32"))
    print(f"epoch A : {earlier['mobility']:>9} · {earlier['composition']['vegetationPct']}% vegetation", flush=True)
    print(f"epoch B : {current['mobility']:>9} · {current['composition']['vegetationPct']}% vegetation", flush=True)

    command = [
        sys.executable, str(ROOT / "scripts" / "compare_collections.py"),
        "--previous", str(outdir / "epoch-a_ndvi_cog.tif"),
        "--current", str(outdir / "epoch-b_ndvi_cog.tif"),
        "--out", str(outdir / "change-report.json"),
    ]
    print()
    return subprocess.call(command)


if __name__ == "__main__":
    sys.exit(main())
