"""Product lineage and handling record for the SkyWatch ISR processing line.

Every published product is accompanied by a manifest that answers the questions a
data-handling review asks about a piece of imagery:

* **Provenance** - which object in which store it came from, its size and ETag at
  the time of processing, so the input can be re-fetched and re-verified.
* **Processing** - processor name and version, the software stack, the band
  selection and the product profile applied.
* **Content** - index statistics, terrain composition and the mobility screen.
* **Handling** - the operator-supplied caveat (default ``UNCLASSIFIED``), the
  retention window and the region the work was done in.

The module is pure Python with no cloud dependencies so it can be unit-tested and
so the record shape stays stable across deployments.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

#: Bumped when the manifest shape or processing defaults change. Consumers should
#: read this before assuming which keys exist.
PROCESSOR_VERSION = "2.0.0"

#: Default caveat applied to every product unless the operator overrides it.
DEFAULT_CAVEAT = "UNCLASSIFIED"

MANIFEST_PREFIX_DEFAULT = "manifests/"


def handling_caveat(env: Optional[Mapping[str, str]] = None) -> str:
    """Operator-supplied handling caveat for the products this line emits.

    Set ``HANDLING_CAVEAT`` on the worker to stamp a real caveat per deployment
    (for example a releasability statement). The default is deliberately the
    most permissive string, never a guess at something more restrictive.
    """
    source = env if env is not None else os.environ
    value = (source.get("HANDLING_CAVEAT") or "").strip()
    return value or DEFAULT_CAVEAT


def retention_policy(days: Optional[int] = None, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The retention window recorded on every product."""
    source = env if env is not None else os.environ
    if days is None:
        try:
            days = int(source.get("RECORD_TTL_DAYS", "30"))
        except (TypeError, ValueError):
            days = 30
    return {"recordTtlDays": days, "productExpiryDays": days, "enforcedBy": "s3 lifecycle + dynamodb ttl"}


def build_manifest(
    *,
    image_id: str,
    source: Mapping[str, Any],
    product: Mapping[str, Any],
    indices: Mapping[str, Any],
    terrain: Mapping[str, Any],
    mobility: Mapping[str, Any],
    processing: Mapping[str, Any],
    region: str,
    generated_at: Optional[str] = None,
    caveat: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the lineage manifest written beside every published product.

    All inputs are plain dictionaries so the caller decides where the values came
    from (S3 head, rasterio profile, the index maths) and this module stays testable.
    """
    return {
        "manifestVersion": 1,
        "processor": {
            "name": "skywatch-isr-line",
            "version": PROCESSOR_VERSION,
            "generatedAt": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "region": region,
        },
        "collection": {
            "imageId": image_id,
            "sourceBucket": source.get("bucket"),
            "sourceKey": source.get("key"),
            "sourceSizeBytes": source.get("sizeBytes"),
            "sourceEtag": source.get("etag"),
        },
        "product": {
            "bucket": product.get("bucket"),
            "key": product.get("key"),
            "sizeBytes": product.get("sizeBytes"),
            "previewKey": product.get("previewKey"),
            "format": product.get("format", "Cloud-Optimized GeoTIFF"),
            "profile": dict(product.get("profile") or {}),
        },
        "analysis": {
            "indices": dict(indices),
            "terrain": dict(terrain),
            "mobility": dict(mobility),
        },
        "processing": dict(processing),
        "handling": {
            "caveat": caveat or DEFAULT_CAVEAT,
            "retention": retention_policy(),
            "personData": "none - products describe ground, not people",
        },
    }


def manifest_key(image_id: str, prefix: str = MANIFEST_PREFIX_DEFAULT) -> str:
    """Deterministic manifest object key for a collection."""
    return f"{prefix}{image_id}_manifest.json"
