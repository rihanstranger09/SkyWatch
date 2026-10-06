"""Tests for the defence-analysis layer: terrain composition, mobility screening,
product lineage manifests and cross-epoch change detection.

These are the functions that turn an index grid into a statement about ground and
into a record an analyst can be held to, so they are covered directly rather than
only through the worker.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from src import change, lineage
from src.indices import terrain_composition, trafficability


# --------------------------------------------------------------------------- #
# terrain composition
# --------------------------------------------------------------------------- #
def test_composition_splits_a_known_scene():
    # 100 pixels: 50 canopy, 30 bare, 20 open water (NDWI above the cut-off)
    ndvi = np.concatenate([np.full(50, 0.7), np.full(30, 0.02), np.full(20, 0.05)]).astype("float32")
    ndwi = np.concatenate([np.full(50, -0.4), np.full(30, -0.3), np.full(20, 0.45)]).astype("float32")

    composition = terrain_composition(ndvi, ndwi)

    assert composition["vegetationPct"] == 50.0
    assert composition["bareGroundPct"] == 30.0
    assert composition["waterPct"] == 20.0
    assert composition["analysedPixelPct"] == 100.0


def test_composition_ignores_unwritten_pixels():
    ndvi = np.full((10, 10), np.nan, dtype="float32")
    ndvi[:5, :] = 0.5  # half the scene carries data

    composition = terrain_composition(ndvi)

    assert composition["analysedPixelPct"] == 50.0
    assert composition["vegetationPct"] == 100.0, "percentages are of analysed ground, not of the array"


def test_composition_of_an_empty_scene_is_all_zero():
    composition = terrain_composition(np.full((8, 8), np.nan, dtype="float32"))
    assert composition["analysedPixelPct"] == 0.0
    assert sum(v for k, v in composition.items() if k != "analysedPixelPct") == 0.0


# --------------------------------------------------------------------------- #
# mobility screening
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "composition,expected",
    [
        ({"waterPct": 60.0, "vegetationPct": 10.0, "bareGroundPct": 20.0, "sparsePct": 10.0}, "NO GO"),
        ({"waterPct": 20.0, "vegetationPct": 10.0, "bareGroundPct": 60.0, "sparsePct": 10.0}, "RESTRICTED"),
        ({"waterPct": 0.0, "vegetationPct": 82.0, "bareGroundPct": 5.0, "sparsePct": 3.0}, "RESTRICTED"),
        ({"waterPct": 0.0, "vegetationPct": 45.0, "bareGroundPct": 30.0, "sparsePct": 25.0}, "SLOW GO"),
        ({"waterPct": 0.0, "vegetationPct": 12.0, "bareGroundPct": 70.0, "sparsePct": 18.0}, "GO"),
    ],
)
def test_mobility_classes(composition, expected):
    verdict = trafficability(composition)
    assert verdict["class"] == expected
    assert verdict["reason"], "a class without a reason is not usable in a brief"


def test_mobility_handles_missing_keys():
    assert trafficability({})["class"] == "GO"


# --------------------------------------------------------------------------- #
# lineage manifest
# --------------------------------------------------------------------------- #
def _manifest(**overrides):
    kwargs = dict(
        image_id="scene-01",
        source={"bucket": "skywatch-isr-collections-1", "key": "collections/scene-01.tif",
                "sizeBytes": 900, "etag": "abc123"},
        product={"bucket": "skywatch-isr-products-1", "key": "products/scene-01_ndvi_cog.tif",
                 "sizeBytes": 463090, "previewKey": "previews/scene-01_ndvi.png",
                 "profile": {"crs": "EPSG:4326", "width": 256}},
        indices={"ndvi": {"mean": 0.55}, "ndwi": {"mean": -0.51}},
        terrain={"vegetationPct": 36.72, "waterPct": 4.18},
        mobility={"class": "SLOW GO", "reason": "mixed vegetation cover"},
        processing={"bands": {"red": 1, "green": 2, "nir": 4}, "durationMs": 137},
        region="ap-south-1",
    )
    kwargs.update(overrides)
    return lineage.build_manifest(**kwargs)


def test_manifest_carries_provenance_content_and_handling():
    manifest = _manifest()

    assert manifest["manifestVersion"] == 1
    assert manifest["collection"]["sourceEtag"] == "abc123"
    assert manifest["product"]["sizeBytes"] == 463090
    assert manifest["analysis"]["mobility"]["class"] == "SLOW GO"
    assert manifest["analysis"]["terrain"]["vegetationPct"] == 36.72
    assert manifest["processor"]["version"] == lineage.PROCESSOR_VERSION
    assert manifest["processor"]["region"] == "ap-south-1"
    assert manifest["handling"]["caveat"] == lineage.DEFAULT_CAVEAT
    assert "ground" in manifest["handling"]["personData"]


def test_manifest_records_the_operator_caveat():
    manifest = _manifest(caveat="TRAINING USE ONLY")
    assert manifest["handling"]["caveat"] == "TRAINING USE ONLY"


def test_manifest_is_json_serialisable():
    assert json.loads(json.dumps(_manifest()))["analysis"]["indices"]["ndvi"]["mean"] == 0.55


def test_caveat_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("HANDLING_CAVEAT", raising=False)
    assert lineage.handling_caveat() == lineage.DEFAULT_CAVEAT

    monkeypatch.setenv("HANDLING_CAVEAT", "RECREATE BEFORE RELEASE")
    assert lineage.handling_caveat() == "RECREATE BEFORE RELEASE"

    monkeypatch.setenv("HANDLING_CAVEAT", "   ")
    assert lineage.handling_caveat() == lineage.DEFAULT_CAVEAT, "blank must not become an empty caveat"


def test_retention_policy_reads_the_configured_window(monkeypatch):
    monkeypatch.setenv("RECORD_TTL_DAYS", "14")
    assert lineage.retention_policy()["recordTtlDays"] == 14

    monkeypatch.setenv("RECORD_TTL_DAYS", "not-a-number")
    assert lineage.retention_policy()["recordTtlDays"] == 30


def test_manifest_key_is_deterministic():
    assert lineage.manifest_key("scene-01") == "manifests/scene-01_manifest.json"
    assert lineage.manifest_key("scene-01", "audit/") == "audit/scene-01_manifest.json"


# --------------------------------------------------------------------------- #
# change detection
# --------------------------------------------------------------------------- #
def test_change_detection_reports_loss_gain_and_verdict():
    previous = np.full((100, 100), 0.6, dtype="float32")
    current = previous.copy()
    current[:40, :] = 0.1   # 40% of the scene cleared
    current[80:, :] = 0.9   # 20% greened up

    result = change.compare(previous, current)

    assert result["lossPct"] == 40.0
    assert result["gainPct"] == 20.0
    assert result["status"] == "SURFACE LOSS"
    assert result["meanDelta"] < 0
    assert result["comparedPixelPct"] == 100.0


def test_change_detection_flags_a_quiet_scene():
    previous = np.full((50, 50), 0.5, dtype="float32")
    current = previous + 0.02  # below the threshold everywhere

    result = change.compare(previous, current)

    assert result["status"] == "NO SIGNIFICANT CHANGE"
    assert result["changedPct"] == 0.0


def test_change_detection_refuses_mismatched_grids():
    with pytest.raises(ValueError, match="not comparable"):
        change.compare(np.zeros((10, 10)), np.zeros((10, 12)))


def test_change_detection_ignores_pixels_missing_from_either_epoch():
    previous = np.full((20, 20), 0.5, dtype="float32")
    current = previous.copy()
    current[:10, :] = np.nan

    result = change.compare(previous, current)

    assert result["comparedPixelPct"] == 50.0
    assert result["status"] == "NO SIGNIFICANT CHANGE"


def test_change_detection_on_an_empty_overlap():
    result = change.compare(np.full((5, 5), np.nan, dtype="float32"), np.zeros((5, 5), dtype="float32"))
    assert result["status"] == "NO OVERLAP"
    assert result["meanDelta"] is None


def test_statistic_deltas():
    deltas = change.compare_stats({"mean": 0.4, "max": 0.8, "valid_pixel_pct": 100.0},
                                  {"mean": 0.55, "max": 0.9, "valid_pixel_pct": 98.0})
    assert deltas["meanDelta"] == 0.15
    assert deltas["maxDelta"] == pytest.approx(0.1)
    assert deltas["validPixelPctDelta"] == -2.0


def test_statistic_deltas_tolerate_missing_values():
    assert change.compare_stats({"mean": None}, {"mean": 0.5})["meanDelta"] is None
