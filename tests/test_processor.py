"""Unit tests for the spectral-index maths.

These run in ~50 ms with nothing but NumPy installed - no AWS credentials, no
GDAL, no network - which is exactly why the formulas live in ``src/indices.py``.
"""

import numpy as np
import pytest

from src.indices import (
    NDVI_MAX,
    NDVI_MIN,
    compute_ndvi,
    compute_ndwi,
    index_stats,
    ndvi_stats,
    ndvi_to_rgb,
    normalize_band,
    stacking_shape,
)


# --------------------------------------------------------------------------- #
# NDVI - the two checks from the original specification are preserved verbatim
# --------------------------------------------------------------------------- #
def test_ndvi_range():
    red = np.array([[0.1, 0.2], [0.3, 0.4]], dtype="float32")
    nir = np.array([[0.5, 0.6], [0.7, 0.8]], dtype="float32")
    ndvi = compute_ndvi(red, nir)

    assert ndvi.min() >= -1.0
    assert ndvi.max() <= 1.0
    assert np.isclose(ndvi[0, 0], (0.5 - 0.1) / (0.5 + 0.1), atol=1e-4)


def test_zero_division_guard():
    red = np.zeros((2, 2), dtype="float32")
    nir = np.zeros((2, 2), dtype="float32")
    ndvi = compute_ndvi(red, nir)

    assert not np.isnan(ndvi).any()
    assert not np.isinf(ndvi).any()
    assert np.all(ndvi == 0.0)


def test_ndvi_saturates_at_extremes():
    ndvi = compute_ndvi(np.array([[1.0, 0.0]], dtype="float32"), np.array([[0.0, 1.0]], dtype="float32"))
    assert np.isclose(ndvi[0, 0], NDVI_MIN)
    assert np.isclose(ndvi[0, 1], NDVI_MAX)


def test_ndvi_is_higher_for_vegetation():
    water = compute_ndvi(np.array([[0.30]], dtype="float32"), np.array([[0.05]], dtype="float32"))
    canopy = compute_ndvi(np.array([[0.08]], dtype="float32"), np.array([[0.65]], dtype="float32"))
    assert float(water[0, 0]) < 0.0 < float(canopy[0, 0])


def test_ndvi_keeps_nodata_as_nan():
    red = np.array([[0.1, np.nan]], dtype="float32")
    nir = np.array([[0.4, np.nan]], dtype="float32")
    ndvi = compute_ndvi(red, nir)
    assert np.isfinite(ndvi[0, 0])
    assert np.isnan(ndvi[0, 1])


def test_ndvi_preserves_shape_and_dtype():
    red = np.random.default_rng(0).random((7, 5)).astype("float32")
    nir = np.random.default_rng(1).random((7, 5)).astype("float32")
    ndvi = compute_ndvi(red, nir)
    assert ndvi.shape == (7, 5)
    assert ndvi.dtype == np.float32


def test_ndvi_accepts_uint16_input():
    red = np.array([[1000, 5000]], dtype="uint16")
    nir = np.array([[8000, 6000]], dtype="uint16")
    ndvi = compute_ndvi(red, nir)
    assert ndvi.dtype == np.float32
    assert 0.0 < float(ndvi[0, 0]) <= 1.0


# --------------------------------------------------------------------------- #
# NDWI
# --------------------------------------------------------------------------- #
def test_ndwi_formula():
    green = np.array([[0.24]], dtype="float32")
    nir = np.array([[0.06]], dtype="float32")
    ndwi = compute_ndwi(green, nir)
    assert np.isclose(ndwi[0, 0], (0.24 - 0.06) / (0.24 + 0.06), atol=1e-4)


def test_ndwi_zero_division_guard():
    ndwi = compute_ndwi(np.zeros((3, 3), dtype="float32"), np.zeros((3, 3), dtype="float32"))
    assert np.all(ndwi == 0.0)


def test_ndwi_positive_over_water():
    water = compute_ndwi(np.array([[0.20]], dtype="float32"), np.array([[0.05]], dtype="float32"))
    land = compute_ndwi(np.array([[0.10]], dtype="float32"), np.array([[0.45]], dtype="float32"))
    assert float(water[0, 0]) > 0.0 > float(land[0, 0])


# --------------------------------------------------------------------------- #
# Band normalisation
# --------------------------------------------------------------------------- #
def test_normalize_band_scales_uint16_to_unit_range():
    band = np.array([[0, 32767, 65535]], dtype="uint16")
    out = normalize_band(band, dtype_max=65535)
    assert out.dtype == np.float32
    assert np.allclose(out, [[0.0, 0.4999, 1.0]], atol=1e-4)


def test_normalize_band_applies_reflectance_scale_factor():
    band = np.array([[1, 10000, 20000]], dtype="float32")
    out = normalize_band(band, scale_factor=0.0001)
    assert np.allclose(out, [[0.0001, 1.0, 2.0]], atol=1e-6)


def test_normalize_band_masks_nodata_to_nan():
    band = np.array([[1, 0, 250]], dtype="uint16")
    out = normalize_band(band, nodata=0, dtype_max=255)
    assert np.isnan(out[0, 1])
    assert np.isfinite(out[0, 0]) and np.isfinite(out[0, 2])


# --------------------------------------------------------------------------- #
# Statistics + preview ramp
# --------------------------------------------------------------------------- #
def test_index_stats_ignores_nan_pixels():
    ndvi = np.array([[0.5, 0.5, np.nan]], dtype="float32")
    stats = index_stats(ndvi)
    assert stats["mean"] == pytest.approx(0.5, abs=1e-6)
    assert stats["valid_pixel_pct"] == pytest.approx(66.667, abs=0.01)


def test_index_stats_all_nan_is_safe():
    stats = index_stats(np.full((4, 4), np.nan, dtype="float32"))
    assert stats == {"mean": None, "min": None, "max": None, "std": None, "valid_pixel_pct": 0.0}


def test_ndvi_stats_alias_matches_index_stats():
    ndvi = np.linspace(-1.0, 1.0, 9, dtype="float32").reshape(3, 3)
    assert ndvi_stats(ndvi) == index_stats(ndvi)


def test_ndvi_to_rgb_shape_dtype_and_clipping():
    ndvi = np.array([[-5.0, 0.0, 5.0], [np.nan, 0.5, -0.5]], dtype="float32")
    rgb = ndvi_to_rgb(ndvi)
    assert rgb.shape == (3, 2, 3)
    assert rgb.dtype == np.uint8
    assert rgb.min() >= 0 and rgb.max() <= 255


def test_ndvi_to_rgb_ramps_from_red_to_green():
    """Water -> soil -> canopy must shift steadily towards green (higher G/R ratio)."""
    water = ndvi_to_rgb(np.array([[-0.6]], dtype="float32"))
    bare_soil = ndvi_to_rgb(np.array([[0.05]], dtype="float32"))
    canopy = ndvi_to_rgb(np.array([[0.85]], dtype="float32"))

    def green_red_ratio(rgb: np.ndarray) -> float:
        return float(rgb[1, 0, 0]) / max(float(rgb[0, 0, 0]), 1.0)

    assert green_red_ratio(water) < green_red_ratio(bare_soil) < green_red_ratio(canopy)
    assert int(canopy[0, 0, 0]) < int(bare_soil[0, 0, 0])  # vegetation reflects less red


def test_stacking_shape_validates_inputs():
    a = np.zeros((4, 5), dtype="float32")
    b = np.zeros((4, 5), dtype="float32")
    assert stacking_shape(a, b) == (4, 5)
    with pytest.raises(ValueError):
        stacking_shape(a, np.zeros((5, 4), dtype="float32"))
