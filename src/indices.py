"""Spectral index maths for the SkyWatch ISR processing line.

Deliberately free of boto3 / GDAL / rasterio imports: this module only needs
NumPy, which means the formulas can be unit-tested in milliseconds, in CI, with
no AWS credentials and no geospatial native dependencies.

Conventions
-----------
* Bands arrive as 2-D NumPy arrays (or masked arrays already filled with NaN).
* Reflectance-like inputs live in 0..1 after :func:`normalize_band`.
* Outputs keep the input shape and are ``float32``.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np

NDVI_MIN = -1.0
NDVI_MAX = 1.0

#: Colour ramp (red -> amber -> yellow-green -> deep green) used for PNG previews.
NDVI_COLOR_STOPS: Tuple[Tuple[float, Tuple[float, float, float]], ...] = (
    (0.00, (0.55, 0.06, 0.13)),  # deep water / cloud shadow
    (0.22, (0.78, 0.28, 0.10)),  # shallow water, wet soil
    (0.38, (0.90, 0.55, 0.16)),  # bare soil, urban
    (0.52, (0.94, 0.84, 0.30)),  # sparse / senescent vegetation
    (0.68, (0.52, 0.78, 0.26)),  # healthy canopy
    (0.84, (0.09, 0.44, 0.16)),  # dense vegetation
    (1.00, (0.02, 0.24, 0.12)),  # peak canopy
)


def to_float32(band: np.ndarray) -> np.ndarray:
    """Return ``band`` as a contiguous float32 array (no copy when already float32)."""
    return np.asarray(band, dtype="float32")


def normalize_band(
    band: np.ndarray,
    nodata: Optional[float] = None,
    dtype_max: Optional[float] = None,
    scale_factor: Optional[float] = None,
    add_offset: Optional[float] = None,
) -> np.ndarray:
    """Scale a raw raster band into reflectance-like 0..1 values.

    ``nodata`` pixels become ``NaN`` so they never pollute the index statistics.
    """
    arr = to_float32(band)
    if scale_factor is not None:
        arr = arr * float(scale_factor)
    if add_offset is not None:
        arr = arr + float(add_offset)
    if dtype_max is not None and float(dtype_max) > 0:
        arr = arr / float(dtype_max)
    if nodata is not None:
        arr = np.where(arr == float(nodata), np.float32("nan"), arr)
    return arr.astype("float32", copy=False)


def compute_ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Normalized Difference Vegetation Index: ``(NIR - RED) / (NIR + RED)``.

    Pixels where the denominator is exactly zero resolve to ``0.0`` instead of
    raising or producing ``inf``; NaN (nodata) pixels stay NaN.
    """
    red_f = to_float32(red)
    nir_f = to_float32(nir)
    denom = nir_f + red_f
    safe = np.where(denom == 0, np.float32(1.0), denom)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        ndvi = (nir_f - red_f) / safe
    ndvi = np.where(denom == 0, np.float32(0.0), ndvi)
    return np.clip(ndvi, NDVI_MIN, NDVI_MAX).astype("float32", copy=False)


def compute_ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """McFeeters Normalized Difference Water Index: ``(GREEN - NIR) / (GREEN + NIR)``."""
    green_f = to_float32(green)
    nir_f = to_float32(nir)
    denom = green_f + nir_f
    safe = np.where(denom == 0, np.float32(1.0), denom)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        ndwi = (green_f - nir_f) / safe
    ndwi = np.where(denom == 0, np.float32(0.0), ndwi)
    return np.clip(ndwi, NDVI_MIN, NDVI_MAX).astype("float32", copy=False)


def valid_mask(index: np.ndarray) -> np.ndarray:
    """Boolean mask of finite (non-nodata) pixels."""
    return np.isfinite(np.asarray(index, dtype="float32"))


def index_stats(index: np.ndarray) -> Dict[str, Optional[float]]:
    """Summary statistics over finite pixels only (safe for DynamoDB storage)."""
    arr = to_float32(index)
    mask = valid_mask(arr)
    valid = arr[mask]
    total = int(arr.size)
    if valid.size == 0:
        return {
            "mean": None,
            "min": None,
            "max": None,
            "std": None,
            "valid_pixel_pct": 0.0,
        }
    return {
        "mean": round(float(valid.mean()), 6),
        "min": round(float(valid.min()), 6),
        "max": round(float(valid.max()), 6),
        "std": round(float(valid.std()), 6),
        "valid_pixel_pct": round(100.0 * float(valid.size) / max(total, 1), 3),
    }


def ndvi_stats(ndvi: np.ndarray) -> Dict[str, Optional[float]]:
    """Backwards-compatible alias of :func:`index_stats` for NDVI arrays."""
    return index_stats(ndvi)


def ndvi_to_rgb(index: np.ndarray, stops: Sequence = NDVI_COLOR_STOPS) -> np.ndarray:
    """Map an index in -1..1 to an ``uint8`` RGB preview array of shape (3, H, W)."""
    arr = to_float32(index)
    scaled = (np.clip(np.nan_to_num(arr, nan=-1.0), NDVI_MIN, NDVI_MAX) + 1.0) / 2.0
    positions = np.array([stop[0] for stop in stops], dtype="float32")
    channels = []
    for channel in range(3):
        values = np.array([stop[1][channel] for stop in stops], dtype="float32")
        channels.append(np.interp(scaled, positions, values))
    rgb = np.stack(channels, axis=0)
    return np.clip(rgb * 255.0, 0, 255).astype("uint8")


def band_stats(band: np.ndarray) -> Dict[str, float]:
    """Lightweight min/max/mean for a raw band (used in the metadata record)."""
    arr = to_float32(band)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "min": round(float(finite.min()), 6),
        "max": round(float(finite.max()), 6),
        "mean": round(float(finite.mean()), 6),
    }


def stacking_shape(*arrays: np.ndarray) -> Tuple[int, int]:
    """Validate that all inputs share one shape and return it (rows, cols)."""
    shapes: Iterable[Tuple[int, int]] = {np.asarray(a).shape for a in arrays}  # type: ignore[assignment]
    unique = set(shapes)
    if len(unique) != 1:
        raise ValueError(f"band shapes must match exactly, got {sorted(unique)}")
    return unique.pop()


# --------------------------------------------------------------------------- #
# Terrain analysis - the decision-support layer
#
# An index grid on its own tells an analyst very little. These two functions turn
# it into statements about ground: what fraction of the area is under canopy,
# vegetation-free, or wet, and whether that ground is likely to support movement.
#
# Thresholds are conventional and deliberately coarse - this is a screening aid
# for imagery triage, not a substitute for a terrain analysis team.
# --------------------------------------------------------------------------- #

#: NDVI cut-offs used for the composition split.
VEGETATION_NDVI = 0.35
SPARSE_NDVI = 0.10
#: NDWI cut-off for open water / saturated ground.
WATER_NDWI = 0.10


def terrain_composition(
    ndvi: np.ndarray,
    ndwi: Optional[np.ndarray] = None,
    vegetation_threshold: float = VEGETATION_NDVI,
    sparse_threshold: float = SPARSE_NDVI,
    water_threshold: float = WATER_NDWI,
) -> Dict[str, float]:
    """Split a scene into canopy / bare ground / water / other, in percent.

    Only finite pixels are counted, so masked or unwritten areas cannot fake a
    composition figure.
    """
    ndvi = to_float32(ndvi)
    finite = np.isfinite(ndvi)
    total = int(np.count_nonzero(finite))
    if total == 0:
        return {"vegetationPct": 0.0, "bareGroundPct": 0.0, "waterPct": 0.0,
                "sparsePct": 0.0, "otherPct": 0.0, "analysedPixelPct": 0.0}

    water = np.zeros_like(finite)
    if ndwi is not None:
        ndwi = to_float32(ndwi)
        water = finite & np.isfinite(ndwi) & (ndwi > water_threshold)

    vegetation = finite & ~water & (ndvi > vegetation_threshold)
    sparse = finite & ~water & (ndvi > sparse_threshold) & (ndvi <= vegetation_threshold)
    bare = finite & ~water & (ndvi <= sparse_threshold)
    other = finite & ~vegetation & ~sparse & ~bare & ~water

    pct = lambda mask: round(100.0 * float(np.count_nonzero(mask)) / total, 2)  # noqa: E731
    return {
        "vegetationPct": pct(vegetation),
        "bareGroundPct": pct(bare),
        "waterPct": pct(water),
        "sparsePct": pct(sparse),
        "otherPct": pct(other),
        "analysedPixelPct": round(100.0 * total / ndvi.size, 2),
    }


def trafficability(composition: Dict[str, float]) -> Dict[str, str]:
    """Screen a :func:`terrain_composition` result for likely vehicle mobility.

    Returns a class and the reason for it. Classes are advisory:

    ``NO GO``        standing water dominates the scene
    ``RESTRICTED``   significant water, or closed canopy with no clearings
    ``SLOW GO``      mixed ground - expect reduced cross-country speed
    ``GO``           open or sparsely vegetated ground
    """
    water = float(composition.get("waterPct") or 0.0)
    vegetation = float(composition.get("vegetationPct") or 0.0)
    bare = float(composition.get("bareGroundPct") or 0.0)
    sparse = float(composition.get("sparsePct") or 0.0)

    if water >= 35.0:
        return {"class": "NO GO", "reason": f"{water:.1f}% standing water in the scene"}
    if water >= 12.0:
        return {"class": "RESTRICTED", "reason": f"water present ({water:.1f}% of scene)"}
    if vegetation >= 70.0 and bare + sparse < 10.0:
        return {"class": "RESTRICTED", "reason": f"closed canopy, few clearings ({vegetation:.1f}% vegetation)"}
    if vegetation >= 35.0:
        return {"class": "SLOW GO", "reason": f"mixed vegetation cover ({vegetation:.1f}% vegetation)"}
    return {"class": "GO", "reason": f"predominantly open ground ({bare + sparse:.1f}% bare or sparse)"}
