#!/usr/bin/env python3
"""Render the dashboard's NDVI assets from the pipeline's own maths.

Produces (into ``frontend/assets/``):

* ``ndvi-matrix.b64``   - 128x128 float32 NDVI, base64 (drives the interactive canvas + tooltip)
* ``ndwi-matrix.b64``   - 128x128 float32 NDWI, base64 (view toggle)
* ``ndvi-preview.png``  - 256x256 colour-mapped NDVI thumbnail (also embedded in the dashboard)
* ``true-colour.png``   - 256x256 synthetic true-colour crop (view toggle)
* ``scene-meta.json``   - geometry + statistics used by the dashboard

Everything is derived with the *same* functions the Lambda uses (``src/indices.py``),
so the dashboard shows a genuine pipeline output rather than a decorative graphic.

    python frontend/make_ndvi_preview.py
"""

from __future__ import annotations

import base64
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.indices import (  # noqa: E402
    compute_ndvi,
    compute_ndwi,
    index_stats,
    ndvi_to_rgb,
    terrain_composition,
    trafficability,
)

ASSETS = ROOT / "frontend" / "assets"
HERO_SIZE = 512
THUMB_SIZE = 256
MATRIX_SIZE = 128
ORIGIN = (77.59, 12.97)  # Bengaluru
PIXEL_SIZE = 0.0001


def hillshade(terrain: np.ndarray, azimuth: float = 315.0, altitude: float = 45.0, z_factor: float = 45.0) -> np.ndarray:
    """Classic relief shading: sun in the north-west, 45 degrees above the horizon."""
    dy, dx = np.gradient(terrain.astype("float32"))
    slope = np.arctan(np.hypot(dx, dy) * z_factor)
    aspect = np.arctan2(dy, -dx)
    shade = (
        np.sin(np.radians(altitude)) * np.cos(slope)
        + np.cos(np.radians(altitude)) * np.sin(slope) * np.cos(np.radians(azimuth) - aspect)
    )
    span = max(float(np.ptp(shade)), 1e-6)
    return np.clip(0.55 + 0.45 * (shade - float(shade.min())) / span, 0.0, 1.0).astype("float32")


def build_hero_scene(size: int = HERO_SIZE, seed: int = 21) -> dict:
    """A richer synthetic scene than the test fixture: river, lake, forest, farmland, city.

    Band reflectance is modelled so the resulting NDVI spans a realistic range
    (about -0.5 over the urban core to +0.85 over dense canopy).
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype("float32")
    nx, ny = xx / size, yy / size

    # Rolling terrain (drives the hillshade on the true-colour view).
    terrain = (
        0.5 * np.sin(5.1 * nx + 1.1) * np.cos(4.3 * ny - 0.6)
        + 0.22 * np.sin(13.0 * nx + 0.4) * np.cos(9.0 * ny)
        + 0.12 * np.sin(23.0 * nx) * np.sin(17.0 * ny)
    )

    # A slimmer, more natural river plus a lake broadening near its mouth.
    river_centre = 0.60 + 0.085 * np.sin(2.6 * nx * np.pi + 0.4) + 0.022 * np.sin(7.0 * nx * np.pi)
    river = np.exp(-((ny - river_centre) ** 2) / (2 * 0.011**2))
    lake = np.exp(-(((nx - 0.15) ** 2) / (2 * 0.062**2) + ((ny - 0.70) ** 2) / (2 * 0.030**2)))
    water = np.clip(river + lake * 1.05, 0.0, 1.0)
    riparian = np.clip(np.exp(-((ny - river_centre) ** 2) / (2 * 0.075**2)) - water, 0.0, 1.0)

    # Forest belt across the north, with fractal-ish edges from smoothed noise.
    noise = rng.normal(0.0, 1.0, (size, size)).astype("float32")
    for _ in range(4):  # fast smoothing pass: several box blurs
        noise = (
            noise
            + np.roll(noise, 1, 0) + np.roll(noise, -1, 0)
            + np.roll(noise, 1, 1) + np.roll(noise, -1, 1)
        ) / 5.0
    noise = (noise - noise.min()) / max(float(np.ptp(noise)), 1e-6)
    forest = np.clip((0.46 - ny) * 2.6 + (noise - 0.5) * 1.35 - terrain * 0.20, 0.0, 1.0)
    forest *= np.clip(0.55 + 0.9 * noise, 0.0, 1.0)  # canopy clumping, not a flat wash

    # Farmland patchwork in the south-east, with bare field boundaries.
    field_rows, field_cols = 6, 8
    crop = rng.uniform(0.18, 1.0, (field_rows, field_cols)).astype("float32")
    row_idx = np.clip((ny * field_rows).astype(int), 0, field_rows - 1)
    col_idx = np.clip((nx * field_cols).astype(int), 0, field_cols - 1)
    field_mask = np.clip((ny - 0.56) * 3.2, 0.0, 1.0) * np.clip((nx - 0.36) * 3.2, 0.0, 1.0)
    field_mask *= (1.0 - water)
    boundary = ((np.mod(nx * field_cols, 1.0) < 0.05) | (np.mod(ny * field_rows, 1.0) < 0.05)).astype("float32")
    fields = crop[row_idx, col_idx] * (1.0 - 0.85 * boundary)

    # Urban core in the south-west: impervious, warm, noisy.
    city = np.exp(-(((nx - 0.30) ** 2) / (2 * 0.075**2) + ((ny - 0.90) ** 2) / (2 * 0.055**2)))
    city_mask = np.clip(city * 1.5, 0.0, 1.0)

    fine = rng.normal(0.0, 0.035, (size, size)).astype("float32")

    # A couple of soft cloud shadows make it read like real orbital imagery.
    shadow = np.clip(
        np.exp(-(((nx - 0.62) ** 2) / (2 * 0.10**2) + ((ny - 0.30) ** 2) / (2 * 0.07**2))) * 0.9
        + np.exp(-(((nx - 0.82) ** 2) / (2 * 0.06**2) + ((ny - 0.72) ** 2) / (2 * 0.05**2))) * 0.7,
        0.0,
        1.0,
    )

    vegetation = np.clip(
        0.20
        + 0.60 * forest
        + 0.46 * fields * field_mask
        + 0.20 * riparian
        - 0.85 * water
        - 0.50 * city_mask
        - 0.22 * shadow
        + 0.08 * terrain
        + fine * 0.5,
        0.0,
        1.0,
    )

    texture = rng.normal(0.0, 0.012, (size, size)).astype("float32")
    # Vegetation absorbs red and reflects NIR (the physical basis of NDVI), while
    # water absorbs almost everything in the visible/NIR - so it must be the
    # darkest surface in the scene, not a bright one.
    water_darkening = 1.0 - 0.72 * water
    red = np.clip((0.055 + 0.30 * (1.0 - vegetation) + texture) * water_darkening, 0.01, 0.90)
    green = np.clip((0.085 + 0.27 * (1.0 - vegetation) + texture) * (1.0 - 0.66 * water), 0.01, 0.90)
    blue = np.clip((0.075 + 0.21 * (1.0 - vegetation) + texture) * (1.0 - 0.58 * water), 0.01, 0.90)
    nir = np.clip((0.115 + 0.80 * vegetation + texture * 1.4) * (1.0 - 0.80 * water), 0.02, 0.98)

    illumination = hillshade(terrain)

    return {
        "red": red.astype("float32"),
        "green": green.astype("float32"),
        "blue": blue.astype("float32"),
        "nir": nir.astype("float32"),
        "water_mask": water.astype("float32"),
        "illumination": illumination,
    }


def downsample_mean(array: np.ndarray, target: int) -> np.ndarray:
    """Mean-pool a 2-D array down to ``target`` x ``target`` (keeps NDVI statistics honest)."""
    size = array.shape[0]
    factor = size // target
    if factor <= 1:
        return array
    trimmed = array[: target * factor, : target * factor]
    return np.nanmean(trimmed.reshape(target, factor, target, factor), axis=(1, 3)).astype("float32")


def stretch_rgb(rgb: np.ndarray, low: float = 1.0, high: float = 99.0, gamma: float = 0.9) -> np.ndarray:
    """Percentile-stretch a reflectance image into a natural-looking 8-bit preview.

    A single shared black/white point keeps the colour balance honest (per-channel
    normalisation would grey out the vegetation), and a mild gain lifts the greens.
    """
    ranks = [rgb[channel][np.isfinite(rgb[channel])] for channel in range(3)]
    combined = np.concatenate(ranks)
    black, white = np.percentile(combined, [low, high])
    scaled = (rgb - black) / max(float(white - black), 1e-6)
    scaled = np.clip(scaled, 0.0, 1.0) ** gamma
    scaled *= np.array([0.98, 1.05, 1.0], dtype="float32").reshape(3, 1, 1)
    return np.clip(scaled * 255.0, 0, 255).astype("uint8")


def shade_and_saturate(rgb: np.ndarray, illumination: np.ndarray, saturation: float = 1.45,
                       shade_strength: float = 0.12) -> np.ndarray:
    """Add terrain relief and colour depth to a stretched RGB preview.

    The hillshade is applied *after* stretching (so it never washes out the
    black point) and the saturation pass pushes the colour away from the local
    luminance, which is what makes vegetation read as green rather than grey.
    """
    image = rgb.astype("float32")
    relief = (illumination - float(illumination.mean())) / max(float(illumination.std()), 1e-6)
    relief = np.clip(1.0 + shade_strength * np.clip(relief, -2.5, 2.5), 0.6, 1.35)
    image = image * relief[None, :, :]

    luminance = image.mean(axis=0, keepdims=True)
    image = np.clip(luminance + saturation * (image - luminance), 0, 255)
    return image.astype("uint8")


def write_png(path: Path, array: np.ndarray) -> None:
    """Write a uint8 RGB image as a PNG, accepting either (H, W, 3) or (3, H, W)."""
    bands = array if array.shape[0] == 3 else np.transpose(array, (2, 0, 1))
    height, width = bands.shape[1], bands.shape[2]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)  # thumbnails are not georeferenced
        with rasterio.open(path, "w", driver="PNG", dtype="uint8", count=3, width=width, height=height) as dst:
            dst.write(bands)


def encode_matrix(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array, dtype="<f4").tobytes()).decode("ascii")


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    scene = build_hero_scene()

    ndvi = compute_ndvi(scene["red"], scene["nir"])
    ndwi = compute_ndwi(scene["green"], scene["nir"])
    composition = terrain_composition(ndvi, ndwi)
    mobility = trafficability(composition)

    # 1. Matrices for the interactive canvas (base64 float32, decoded in the browser).
    ndvi_matrix = downsample_mean(ndvi, MATRIX_SIZE)
    ndwi_matrix = downsample_mean(ndwi, MATRIX_SIZE)
    (ASSETS / "ndvi-matrix.b64").write_text(encode_matrix(ndvi_matrix), encoding="ascii")
    (ASSETS / "ndwi-matrix.b64").write_text(encode_matrix(ndwi_matrix), encoding="ascii")

    # 2. PNG thumbnails (colour-mapped NDVI + true colour with hillshade).
    thumbnail = downsample_mean(ndvi, THUMB_SIZE)
    write_png(ASSETS / "ndvi-preview.png", ndvi_to_rgb(thumbnail))

    illumination = downsample_mean(scene["illumination"], THUMB_SIZE)
    reflectance = np.stack(
        [downsample_mean(scene[band], THUMB_SIZE) for band in ("red", "green", "blue")], axis=0
    )
    write_png(ASSETS / "true-colour.png", shade_and_saturate(stretch_rgb(reflectance), illumination))
    # compact copy for the dashboard's embedded RGB view (kept small on purpose)
    reflectance_128 = np.stack([downsample_mean(scene[band], 128) for band in ("red", "green", "blue")], axis=0)
    write_png(
        ASSETS / "true-colour-128.png",
        shade_and_saturate(stretch_rgb(reflectance_128), downsample_mean(illumination, 128)),
    )

    # 3. Scene metadata rendered by the dashboard.
    stats = index_stats(ndvi)
    water_stats = index_stats(ndwi)
    meta = {
        "generatedBy": "frontend/make_ndvi_preview.py",
        "scene": "synthetic-hero-scene",
        "seed": 21,
        "size": HERO_SIZE,
        "matrixSize": MATRIX_SIZE,
        "crs": "EPSG:4326",
        "origin": list(ORIGIN),
        "pixelSize": PIXEL_SIZE,
        "bounds": [ORIGIN[0], ORIGIN[1] - HERO_SIZE * PIXEL_SIZE, ORIGIN[0] + HERO_SIZE * PIXEL_SIZE, ORIGIN[1]],
        "ndvi": stats,
        "ndwi": water_stats,
        # Terrain composition is produced by the same classifier the worker applies to
        # every collection (src/indices.py:terrain_composition), so the scene shown here
        # and the products written by the line can never drift apart.
        "terrain": composition,
        "mobility": mobility,
        "vegetationPct": composition["vegetationPct"],
        "bareGroundPct": composition["bareGroundPct"],
        "bareSoilPct": composition["bareGroundPct"],  # retained key name
        "waterPct": composition["waterPct"],
        "sparsePct": composition["sparsePct"],
    }
    (ASSETS / "scene-meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print("assets written to frontend/assets/")
    for name in (
        "ndvi-matrix.b64",
        "ndwi-matrix.b64",
        "ndvi-preview.png",
        "true-colour.png",
        "true-colour-128.png",
        "scene-meta.json",
    ):
        path = ASSETS / name
        print(f"  {name:22s} {path.stat().st_size / 1024:8.1f} KiB")
    print(f"  NDVI mean/min/max: {stats['mean']} / {stats['min']} / {stats['max']}")
    print(f"  terrain   : {meta['vegetationPct']}% vegetation · {meta['bareGroundPct']}% bare ground · "
          f"{meta['waterPct']}% water · {meta['sparsePct']}% sparse")
    print(f"  mobility  : {meta['mobility']['class']} - {meta['mobility']['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
