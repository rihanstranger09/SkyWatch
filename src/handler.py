"""AWS Lambda worker for the satellite & drone imagery pipeline.

Triggered by SQS (an S3 ``ObjectCreated`` notification on every ``.tif`` /
``.tiff`` that lands in the raw imagery bucket). For each message the worker:

1. downloads the raster from S3 into ``/tmp``,
2. computes NDVI / NDWI with rasterio + NumPy,
3. writes a Cloud-Optimized GeoTIFF (plus a PNG preview) back to S3,
4. upserts a metadata record in DynamoDB (``PROCESSING`` -> ``SUCCEEDED`` / ``FAILED``),
5. reports per-message failures back to SQS so poison messages reach the DLQ.

All AWS specifics live here; the maths lives in ``indices.py`` so it stays
unit-testable without GDAL, boto3 or credentials.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import time
import urllib.parse
import uuid
import warnings
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.shutil import copy as rio_copy

try:  # deployed layout: handler.py + indices.py sit flat in ${LAMBDA_TASK_ROOT}
    from indices import (
        NDVI_COLOR_STOPS,
        compute_ndvi,
        compute_ndwi,
        index_stats,
        ndvi_to_rgb,
        normalize_band,
    )
except ImportError:  # repo layout: `pytest` / `python scripts/local_e2e.py`
    from src.indices import (  # type: ignore[no-redef]
        NDVI_COLOR_STOPS,
        compute_ndvi,
        compute_ndwi,
        index_stats,
        ndvi_to_rgb,
        normalize_band,
    )

SCHEMA_VERSION = 1

LOG = logging.getLogger("geospatial-processor")
if not LOG.handlers:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3_client = None
_metadata_table = None


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _table():
    global _metadata_table
    if _metadata_table is None:
        _metadata_table = boto3.resource("dynamodb").Table(_env("METADATA_TABLE", "ImageryMetadata"))
    return _metadata_table


def _reset_clients() -> None:
    """Drop cached boto3 clients (used by unit tests and the local moto demo)."""
    global _s3_client, _metadata_table
    _s3_client = None
    _metadata_table = None


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: Any) -> Any:
    """Make NumPy/NaN values JSON- and DynamoDB-safe.

    DynamoDB (via the boto3 resource layer) rejects Python ``float`` and ``NaN``,
    so floating point numbers are rounded and converted to ``Decimal``; non-finite
    values become ``None`` (the attribute is then dropped by :func:`_record`).
    """
    if value is None:
        return None
    if isinstance(value, (np.floating, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return Decimal(str(round(number, 6)))
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        return _clean(value.tolist())
    return value


def image_id_from_key(key: str) -> str:
    """``raw-imagery/Ci-Test_Raster.tif`` -> ``Ci-Test_Raster`` (DynamoDB partition key)."""
    name = os.path.basename(key)
    stem, _ = os.path.splitext(name)
    return stem or name


def parse_s3_notification(payload: Dict[str, Any]) -> Tuple[str, str, Optional[int]]:
    """Extract ``(bucket, key, size)`` from an S3 event envelope.

    Accepts both the SQS-wrapped S3 notification (production path) and a bare S3
    event (handy for ``sam local invoke`` and direct-invocation debugging).
    """
    records = payload.get("Records") or []
    if not records and payload.get("eventSource") == "aws:s3":
        records = [payload]  # bare S3 notification record (no envelope)
    if not records:
        raise ValueError("event payload contains no Records")
    record = records[0]
    if record.get("eventSource") == "aws:s3":
        info = record.get("s3", {})
        bucket = info.get("bucket", {}).get("name")
        raw_key = info.get("object", {}).get("key", "")
        size = info.get("object", {}).get("size")
        key = urllib.parse.unquote_plus(raw_key)
        if not bucket or not key:
            raise ValueError("malformed S3 event: missing bucket or key")
        return bucket, key, _clean(size)
    raise ValueError(f"unsupported inner event source: {record.get('eventSource')}")


def _block_size(width: int, height: int, preferred: int = 512) -> int:
    """Largest power-of-two tile size <= preferred that still fits the raster."""
    limit = max(16, min(width, height, preferred))
    size = 16
    while size * 2 <= limit:
        size *= 2
    return size


def _is_geotiff(key: str) -> bool:
    return key.lower().endswith((".tif", ".tiff"))


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------
def _load_bands(path: str, red_band: int, green_band: int, nir_band: int) -> Dict[str, Any]:
    """Read and normalise the bands we need, returning a small context dict."""
    with rasterio.open(path) as src:
        if src.count < max(red_band, nir_band):
            raise ValueError(f"expected at least {max(red_band, nir_band)} bands, found {src.count}")

        def read(index: int) -> np.ndarray:
            masked = src.read(index, masked=True).astype("float32")
            filled = np.ma.filled(masked, np.float32("nan"))
            dtype = np.dtype(src.dtypes[index - 1])
            scale = float(src.scales[index - 1]) if src.scales else 1.0
            offset = float(src.offsets[index - 1]) if src.offsets else 0.0
            # Sentinel/Landsat style: an explicit scale factor wins over the raw
            # integer range; otherwise fall back to dtype_max (e.g. uint16 -> 65535).
            explicit_scale = scale not in (0.0, 1.0)
            dtype_max = None if explicit_scale else (
                float(np.iinfo(dtype).max) if np.issubdtype(dtype, np.integer) else None
            )
            return normalize_band(
                filled,
                dtype_max=dtype_max,
                scale_factor=scale if explicit_scale else None,
                add_offset=offset if offset not in (0.0,) else None,
            )

        red = read(red_band)
        nir = read(nir_band)
        green = read(green_band) if src.count >= green_band else red
        context = {
            "red": red,
            "nir": nir,
            "green": green,
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "crs": src.crs.to_string() if src.crs else None,
            "transform": src.transform,
            "bounds": [round(float(b), 8) for b in src.bounds],
            "dtypes": list(src.dtypes),
            "nodata": src.nodata,
        }
    return context


def _write_cog(
    raw_path: str,
    cog_path: str,
    context: Dict[str, Any],
    ndvi: np.ndarray,
    ndwi: Optional[np.ndarray],
) -> None:
    """Write a Cloud-Optimized GeoTIFF (NDVI band 1, NDWI band 2 when available)."""
    height, width = ndvi.shape
    block = _block_size(width, height)
    stack = [ndvi] if ndwi is None else [ndvi, ndwi]
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": len(stack),
        "width": width,
        "height": height,
        "crs": context.get("crs"),
        "transform": context.get("transform"),
        "nodata": float("nan"),
        "tiled": True,
        "blockxsize": block,
        "blockysize": block,
        "compress": "DEFLATE",
        "predictor": 2,
        "BIGTIFF": "IF_SAFER",
    }
    with rasterio.open(raw_path, "w", **profile) as dst:
        for index, band in enumerate(stack, start=1):
            dst.write(np.asarray(band, dtype="float32"), index)
            dst.update_tags(index, description="NDVI (NIR-RED)/(NIR+RED)" if index == 1 else "NDWI (GREEN-NIR)/(GREEN+NIR)")

    try:
        rio_copy(
            raw_path,
            cog_path,
            driver="COG",
            COMPRESS="DEFLATE",
            BLOCKSIZE=block,
            OVERVIEWS="AUTO",
            RESAMPLING="AVERAGE",
        )
    except Exception as exc:  # COG driver unavailable -> keep the tiled GeoTIFF
        LOG.warning(json.dumps({"event": "cog_fallback", "error": str(exc)}))
        shutil.copyfile(raw_path, cog_path)


def _write_preview(path: str, ndvi: np.ndarray) -> bool:
    """Write a colour-mapped PNG preview of the NDVI band. Never fatal."""
    try:
        rgb = ndvi_to_rgb(ndvi, NDVI_COLOR_STOPS)
        height, width = ndvi.shape
        with warnings.catch_warnings():
            # A preview PNG is deliberately not georeferenced - drop rasterio's warning.
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(path, "w", driver="PNG", dtype="uint8", count=3, width=width, height=height) as dst:
                dst.write(rgb)
        return True
    except Exception as exc:
        LOG.warning(json.dumps({"event": "preview_failed", "error": str(exc)}))
        return False


def _record(image_id: str, **fields: Any) -> None:
    """Upsert the metadata item (ignoring None values so DynamoDB stays clean)."""
    item = {"ImageId": image_id, "SchemaVersion": SCHEMA_VERSION}
    item.update({key: value for key, value in fields.items() if value is not None})
    _table().put_item(Item=_clean(item))


def process_object(bucket: str, key: str, size_bytes: Optional[int] = None) -> Dict[str, Any]:
    """Run the full pipeline for one S3 object and return the stored metadata."""
    started = time.perf_counter()
    image_id = image_id_from_key(key)
    output_bucket = _env("OUTPUT_BUCKET", "")
    if not output_bucket:
        raise RuntimeError("OUTPUT_BUCKET environment variable is not set")

    output_prefix = _env("OUTPUT_PREFIX", "processed-imagery/")
    preview_prefix = _env("PREVIEW_PREFIX", "previews/")
    write_preview = _env("WRITE_PREVIEW", "true").lower() == "true"
    red_band = _env_int("RED_BAND", 1)
    green_band = _env_int("GREEN_BAND", 2)
    nir_band = _env_int("NIR_BAND", 4)

    existing = _table().get_item(Key={"ImageId": image_id}).get("Item")
    if existing and existing.get("Status") == "SUCCEEDED" and (size_bytes in (None, existing.get("SourceSizeBytes"))):
        LOG.info(json.dumps({"event": "already_processed", "image_id": image_id}))
        return dict(existing)

    created_at = existing.get("CreatedAt") if existing else _now().isoformat()
    ttl_seconds = _env_int("RECORD_TTL_DAYS", 30) * 86400
    _record(
        image_id,
        Status="PROCESSING",
        SourceBucket=bucket,
        SourceKey=key,
        SourceSizeBytes=size_bytes,
        OutputBucket=output_bucket,
        CreatedAt=created_at,
        UpdatedAt=_now().isoformat(),
        ExpiresAt=int(time.time()) + ttl_seconds,
    )

    workdir = os.path.join(_env("TMP_DIR", "/tmp"), f"geo-{uuid.uuid4().hex[:12]}")
    os.makedirs(workdir, exist_ok=True)
    local_tif = os.path.join(workdir, "input.tif")
    raw_out = os.path.join(workdir, "indices-raw.tif")
    cog_out = os.path.join(workdir, "indices-cog.tif")
    preview_out = os.path.join(workdir, "ndvi-preview.png")

    try:
        if not _is_geotiff(key):
            raise ValueError(f"unsupported object type (expected .tif/.tiff): {key}")
        LOG.info(json.dumps({"event": "download_start", "bucket": bucket, "key": key}))
        _s3().download_file(bucket, key, local_tif)

        context = _load_bands(local_tif, red_band, green_band, nir_band)
        red, nir, green = context["red"], context["nir"], context["green"]
        ndvi = compute_ndvi(red, nir)
        ndwi = compute_ndwi(green, nir)
        stats = index_stats(ndvi)
        water = index_stats(ndwi)
        cloud_free = float(np.mean(np.isfinite(ndvi)) * 100.0)

        _write_cog(raw_out, cog_out, context, ndvi, ndwi)

        output_key = f"{output_prefix}{image_id}_ndvi_cog.tif"
        _s3().upload_file(cog_out, output_bucket, output_key, ExtraArgs={"ContentType": "image/tiff"})

        preview_key = None
        if write_preview and _write_preview(preview_out, ndvi):
            preview_key = f"{preview_prefix}{image_id}_ndvi.png"
            _s3().upload_file(preview_out, output_bucket, preview_key, ExtraArgs={"ContentType": "image/png"})

        duration_ms = int((time.perf_counter() - started) * 1000)
        record = {
            "Status": "SUCCEEDED",
            "SourceBucket": bucket,
            "SourceKey": key,
            "SourceSizeBytes": size_bytes,
            "OutputBucket": output_bucket,
            "OutputKey": output_key,
            "OutputSizeBytes": os.path.getsize(cog_out),
            "PreviewKey": preview_key,
            "Width": context["width"],
            "Height": context["height"],
            "BandCount": context["count"],
            "Crs": context["crs"],
            "Bounds": context["bounds"],
            "RedBand": red_band,
            "GreenBand": green_band,
            "NirBand": nir_band,
            "NdviMean": stats["mean"],
            "NdviMin": stats["min"],
            "NdviMax": stats["max"],
            "NdviStd": stats["std"],
            "ValidPixelPct": stats["valid_pixel_pct"],
            "NdwiMean": water["mean"],
            "NdwiMin": water["min"],
            "NdwiMax": water["max"],
            "CloudFreePct": round(cloud_free, 3),
            "DurationMs": duration_ms,
            "CreatedAt": created_at,
            "UpdatedAt": _now().isoformat(),
            "ExpiresAt": int(time.time()) + ttl_seconds,
        }
        _record(image_id, **record)
        LOG.info(json.dumps({"event": "processed", "image_id": image_id, "duration_ms": duration_ms, **stats}))
        return record
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        _record(
            image_id,
            Status="FAILED",
            SourceBucket=bucket,
            SourceKey=key,
            SourceSizeBytes=size_bytes,
            ErrorMessage=str(exc)[:1000],
            ErrorType=type(exc).__name__,
            DurationMs=duration_ms,
            CreatedAt=created_at,
            UpdatedAt=_now().isoformat(),
            ExpiresAt=int(time.time()) + ttl_seconds,
        )
        LOG.error(json.dumps({"event": "failed", "image_id": image_id, "error": str(exc)}))
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def process_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one SQS (or raw S3) record."""
    body = record.get("body")
    if isinstance(body, str):
        payload = json.loads(body or "{}")
    elif isinstance(body, dict):
        payload = body
    else:
        payload = record
    bucket, key, size = parse_s3_notification(payload)
    return process_object(bucket, key, size)


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """SQS batch entry point; partial failures are reported back to the queue."""
    records: List[Dict[str, Any]] = list(event.get("Records", []))
    request_id = getattr(context, "aws_request_id", "local")
    LOG.info(json.dumps({"event": "batch_received", "records": len(records), "request_id": request_id}))

    batch_item_failures: List[Dict[str, str]] = []
    for record in records:
        message_id = record.get("messageId") or record.get("message_id") or uuid.uuid4().hex
        try:
            process_record(record)
        except Exception:
            LOG.exception(json.dumps({"event": "record_failed", "message_id": message_id}))
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
