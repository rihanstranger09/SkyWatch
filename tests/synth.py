"""Synthetic GeoTIFF fixtures.

Shared by the unit tests, the CI integration test and the local moto demo so the
whole project can be exercised without a single byte of real satellite imagery.

The scene mimics a Bengaluru-like area: vegetation blobs (high NDVI), bare soil
and a small lake (low/negative NDVI), plus texture noise - which makes NDVI
statistics, previews and assertions meaningful.
"""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np
import rasterio
from rasterio.transform import from_origin

#: lon/lat origin of the demo scene (Bengaluru) - matches the CI integration test.
BENGALURU_ORIGIN: Tuple[float, float] = (77.59, 12.97)
#: ~11 m ground sample distance at this latitude.
DEFAULT_PIXEL_SIZE: float = 0.0001
#: Band order of the synthetic raster (matches the pipeline's band contract).
BAND_ORDER: Sequence[str] = ("red", "green", "blue", "nir")


def build_scene(width: int = 256, height: int = 256, seed: int = 7) -> Dict[str, np.ndarray]:
    """Return ``{red, green, blue, nir}`` float32 arrays in 0..1 reflectance."""
    yy, xx = np.mgrid[0:height, 0:width].astype("float32")
    rng = np.random.default_rng(seed)

    vegetation = np.zeros((height, width), dtype="float32")
    for _ in range(14):
        cx = rng.uniform(0, width)
        cy = rng.uniform(0, height)
        sigma = rng.uniform(18.0, 55.0)
        amplitude = rng.uniform(0.35, 1.0)
        vegetation += amplitude * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * sigma**2)))

    lake = np.exp(
        -(((xx - width * 0.78) ** 2 + (yy - height * 0.26) ** 2) / (2.0 * (width * 0.09) ** 2))
    )
    vegetation = np.clip(vegetation * 1.4 - lake * 1.4, 0.0, 1.0)

    texture = rng.normal(0.0, 0.015, (height, width)).astype("float32")
    red = np.clip(0.10 + 0.22 * (1.0 - vegetation) + texture, 0.01, 0.90)
    green = np.clip(0.13 + 0.18 * (1.0 - vegetation) + 0.06 * lake + texture, 0.01, 0.90)
    blue = np.clip(0.10 + 0.14 * (1.0 - vegetation) + 0.12 * lake + texture, 0.01, 0.90)
    nir = np.clip(0.12 + 0.74 * vegetation + 0.02 * lake + texture * 1.5, 0.02, 0.98)

    return {
        "red": red.astype("float32"),
        "green": green.astype("float32"),
        "blue": blue.astype("float32"),
        "nir": nir.astype("float32"),
    }


def make_geotiff_bytes(
    width: int = 256,
    height: int = 256,
    dtype: str = "float32",
    crs: str = "EPSG:4326",
    origin: Tuple[float, float] = BENGALURU_ORIGIN,
    pixel_size: float = DEFAULT_PIXEL_SIZE,
    bands: Optional[Sequence[str]] = None,
    nodata: Optional[float] = None,
    seed: int = 7,
    scale: Optional[float] = None,
    offset: Optional[float] = None,
) -> bytes:
    """Serialise the synthetic scene to an in-memory GeoTIFF (bytes).

    Integer dtypes are written as ``value * 10000`` (Sentinel/Landsat style) and,
    when ``scale`` is provided, the dataset ``scales`` metadata is set so the
    worker's normalisation path can be verified.
    """
    scene = build_scene(width=width, height=height, seed=seed)
    order = list(bands or BAND_ORDER)
    is_integer = np.issubdtype(np.dtype(dtype), np.integer)
    multiplier = 10000.0 if is_integer else 1.0

    transform = from_origin(origin[0], origin[1], pixel_size, pixel_size)
    profile: Dict[str, object] = {
        "driver": "GTiff",
        "dtype": dtype,
        "width": width,
        "height": height,
        "count": len(order),
        "crs": crs,
        "transform": transform,
        "compress": "DEFLATE",
    }
    if nodata is not None:
        profile["nodata"] = nodata

    buffer = BytesIO()
    with rasterio.open(buffer, "w", **profile) as dst:
        for index, name in enumerate(order, start=1):
            data = np.clip(scene[name] * multiplier, 0, None).astype(dtype)
            dst.write(data, index)
        if scale is not None:
            dst.scales = [float(scale)] * len(order)
        if offset is not None:
            dst.offsets = [float(offset)] * len(order)
    return buffer.getvalue()


def expected_ndvi(seed: int = 7, width: int = 256, height: int = 256) -> np.ndarray:
    """Reference NDVI of the synthetic scene (for assertion tolerance checks)."""
    scene = build_scene(width=width, height=height, seed=seed)
    denominator = scene["nir"] + scene["red"]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(denominator == 0, 0.0, (scene["nir"] - scene["red"]) / denominator)


def jsonable(value: Any) -> Any:
    """Recursively convert DynamoDB's ``Decimal`` values into JSON-friendly floats.

    Both the CI integration test and the local demo read metadata straight out of
    DynamoDB (which returns ``Decimal``) and then serialise a status snapshot.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value
