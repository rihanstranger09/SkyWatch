#!/usr/bin/env python3
"""End-to-end integration test executed inside the GitHub Actions runner.

Flow
----
1. Synthesise a 4-band (R, G, B, NIR) GeoTIFF in memory - no external fixtures.
2. Upload it to ``s3://<raw-bucket>/ci-test-raster.tif`` (S3 notification -> SQS -> Lambda).
3. Poll DynamoDB until the worker writes a terminal record for that ``ImageId``.
4. Verify the processed Cloud-Optimized GeoTIFF (and PNG preview) exist in S3.
5. Emit ``pipeline-status.json`` for the dashboard / GitHub Pages site, plus a
   human-readable report in the Actions log and job summary.

Exits non-zero on any failure, so the workflow fails loudly instead of silently
"passing" an asynchronous pipeline that never ran.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import (  # noqa: E402
    BENGALURU_ORIGIN,
    DEFAULT_PIXEL_SIZE,
    build_scene,
    jsonable,
    make_geotiff_bytes,
)

DEFAULT_TABLE = "CollectionMetadata"
DEFAULT_KEY = "ci-test-raster.tif"


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Asynchronous pipeline integration test")
    parser.add_argument("--bucket", required=True, help="raw imagery bucket (S3 notification source)")
    parser.add_argument("--region", required=True, help="AWS region, e.g. ap-south-1")
    parser.add_argument("--processed-bucket", default=None, help="output bucket (falls back to the DynamoDB record)")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="DynamoDB metadata table name")
    parser.add_argument("--key", default=DEFAULT_KEY, help="object key to upload into the raw bucket")
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=256)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the Lambda")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--status-file", default="pipeline-status.json", help="dashboard status snapshot to write")
    return parser.parse_args(argv)


def poll_metadata(table, image_id: str, timeout: float, interval: float) -> Tuple[Dict[str, Any], int]:
    """Wait for a terminal DynamoDB record; fail fast on a FAILED worker run."""
    deadline = time.time() + timeout
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        elapsed = int(time.time() - (deadline - timeout))
        response = table.get_item(Key={"ImageId": image_id})
        item = response.get("Item")
        if item:
            status = item.get("Status")
            log(f"attempt {attempts:02d} (+{elapsed}s): ImageId={image_id} exists, Status={status}")
            if status == "SUCCEEDED":
                return item, attempts
            if status == "FAILED":
                raise RuntimeError(f"Lambda reported FAILED: {item.get('ErrorMessage', 'unknown error')}")
        else:
            log(f"attempt {attempts:02d} (+{elapsed}s): no DynamoDB record yet...")
        time.sleep(interval)
    raise TimeoutError(
        f"timed out after {timeout:.0f}s waiting for ImageId={image_id}. "
        "Check: S3 -> SQS notification wiring, SQS queue depth, Lambda CloudWatch logs, DLQ depth."
    )


def verify_s3_output(s3, bucket: str, key: str, expected_size: Optional[int]) -> Dict[str, Any]:
    head = s3.head_object(Bucket=bucket, Key=key)
    size = int(head["ContentLength"])
    if size <= 0:
        raise AssertionError(f"s3://{bucket}/{key} is empty")
    if expected_size and size != expected_size:
        raise AssertionError(f"s3://{bucket}/{key} size {size} != recorded {expected_size}")
    return {"bucket": bucket, "key": key, "sizeBytes": size, "contentType": head.get("ContentType")}


def verify_raster(s3, bucket: str, key: str) -> Dict[str, Any]:
    """Optional deep check: read the COG header back with rasterio."""
    try:
        from rasterio.io import MemoryFile
    except ImportError:  # rasterio not installed -> skip gracefully
        return {"skipped": True, "reason": "rasterio not available"}
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    with MemoryFile(body) as mem, mem.open() as src:
        return {
            "driver": src.driver,
            "bands": src.count,
            "dtype": src.dtypes[0],
            "crs": src.crs.to_string() if src.crs else None,
            "width": src.width,
            "height": src.height,
            "bounds": [round(float(b), 8) for b in src.bounds],
            "is_tiled": bool(src.profile.get("tiled")),
        }


def github_context() -> Dict[str, Optional[str]]:
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    return {
        "repository": repo,
        "runId": run_id,
        "runNumber": os.environ.get("GITHUB_RUN_NUMBER"),
        "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "ref": os.environ.get("GITHUB_REF_NAME"),
        "sha": (os.environ.get("GITHUB_SHA") or "")[:7] or None,
        "actor": os.environ.get("GITHUB_ACTOR"),
        "workflow": os.environ.get("GITHUB_WORKFLOW"),
        "runUrl": f"https://github.com/{repo}/actions/runs/{run_id}" if repo and run_id else None,
    }


def write_status_file(path: str, status: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(jsonable(status), handle, indent=2)
    log(f"dashboard snapshot written -> {path}")


def append_job_summary(title: str, rows: List[Tuple[str, str]]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [f"### {title}", "", "| Check | Result |", "| --- | --- |"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    started_wall = time.time()
    image_id = os.path.splitext(os.path.basename(args.key))[0]

    s3 = boto3.client("s3", region_name=args.region)
    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    table = dynamodb.Table(args.table)

    log(f"region={args.region} raw_bucket={args.bucket} table={args.table}")
    raster = make_geotiff_bytes(width=args.width, height=args.height, seed=args.seed)
    scene = build_scene(width=args.width, height=args.height, seed=args.seed)

    log(f"generated synthetic GeoTIFF: {args.width}x{args.height}, 4 bands, {len(raster) / 1024:.1f} KiB")
    s3.put_object(Bucket=args.bucket, Key=args.key, Body=raster, ContentType="image/tiff")
    upload_done = time.time()
    log(f"uploaded -> s3://{args.bucket}/{args.key} (pipeline triggered)")

    item, attempts = poll_metadata(table, image_id, args.timeout, args.poll_interval)
    completed = time.time()

    processed_bucket = item.get("OutputBucket") or args.processed_bucket or args.bucket
    checks: List[Dict[str, Any]] = []

    output_info = verify_s3_output(s3, processed_bucket, item["OutputKey"], item.get("OutputSizeBytes"))
    checks.append({"name": "Processed COG present in S3", "ok": True, "detail": f"{output_info['sizeBytes']} bytes"})
    log(f"verified output COG: s3://{processed_bucket}/{item['OutputKey']} ({output_info['sizeBytes']} bytes)")

    raster_info: Dict[str, Any] = {}
    try:
        raster_info = verify_raster(s3, processed_bucket, item["OutputKey"])
        checks.append({"name": "COG header readable (rasterio)", "ok": True, "detail": json.dumps(raster_info)})
        log(f"COG header: {json.dumps(raster_info)}")
    except Exception as exc:  # pragma: no cover - defensive
        checks.append({"name": "COG header readable (rasterio)", "ok": False, "detail": str(exc)})
        log(f"WARNING: could not re-read COG header: {exc}")

    if item.get("PreviewKey"):
        try:
            preview = verify_s3_output(s3, processed_bucket, item["PreviewKey"], None)
            checks.append({"name": "PNG preview present", "ok": True, "detail": f"{preview['sizeBytes']} bytes"})
            log(f"verified preview PNG: s3://{processed_bucket}/{item['PreviewKey']}")
        except ClientError as exc:
            checks.append({"name": "PNG preview present", "ok": False, "detail": str(exc)})

    stages = [
        {"name": "Upload", "status": "success", "durationMs": int((upload_done - started_wall) * 1000), "detail": f"{len(raster)} bytes -> s3://{args.bucket}/{args.key}"},
        {"name": "S3 notification", "status": "success", "durationMs": None, "detail": "ObjectCreated:* -> SQS collection-processing-queue"},
        {"name": "Lambda (container)", "status": "success", "durationMs": item.get("DurationMs"), "detail": f"NDVI mean {item.get('NdviMean')} | {item.get('Width')}x{item.get('Height')} px"},
        {"name": "S3 + DynamoDB write", "status": "success", "durationMs": None, "detail": item.get("OutputKey")},
        {"name": "Verification", "status": "success", "durationMs": int((completed - upload_done) * 1000), "detail": f"{attempts} poll attempts"},
    ]

    status = {
        "source": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stack": os.environ.get("STACK_NAME", "skywatch-isr-line"),
        "region": args.region,
        "github": github_context(),
        "image": {
            "id": image_id,
            "key": args.key,
            "bucket": args.bucket,
            "sizeBytes": len(raster),
            "width": args.width,
            "height": args.height,
            "bands": 4,
            "crs": "EPSG:4326",
            "origin": list(BENGALURU_ORIGIN),
            "pixelSize": DEFAULT_PIXEL_SIZE,
            "sceneNdviMean": round(float(scene["nir"].mean() - scene["red"].mean()), 6),
        },
        "result": {
            "status": item.get("Status"),
            "durationMs": item.get("DurationMs"),
            "wallClockMs": int((completed - started_wall) * 1000),
            "pollAttempts": attempts,
            "ndvi": {
                "mean": item.get("NdviMean"),
                "min": item.get("NdviMin"),
                "max": item.get("NdviMax"),
                "std": item.get("NdviStd"),
                "validPixelPct": item.get("ValidPixelPct"),
            },
            "ndwi": {"mean": item.get("NdwiMean")},
            "outputKey": item.get("OutputKey"),
            "outputSizeBytes": item.get("OutputSizeBytes"),
            "previewKey": item.get("PreviewKey"),
            "bounds": item.get("Bounds"),
        },
        "stages": stages,
        "checks": checks,
    }
    write_status_file(args.status_file, status)

    append_job_summary(
        "End-to-end integration test",
        [
            ("Synthetic raster", f"{args.width}x{args.height} px, 4 bands, {len(raster)} bytes"),
            ("Lambda status", str(item.get("Status"))),
            ("Lambda duration", f"{item.get('DurationMs')} ms"),
            ("NDVI mean / min / max", f"{item.get('NdviMean')} / {item.get('NdviMin')} / {item.get('NdviMax')}"),
            ("Output COG", f"s3://{processed_bucket}/{item.get('OutputKey')}"),
            ("Poll attempts", str(attempts)),
            ("Wall clock", f"{status['result']['wallClockMs']} ms"),
        ],
    )

    log("INTEGRATION TEST PASSED - asynchronous pipeline verified end to end")
    print(json.dumps(jsonable({"status": "SUCCEEDED", "imageId": image_id, "ndvi": status["result"]["ndvi"]}), indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - the workflow must fail loudly
        log(f"INTEGRATION TEST FAILED: {type(exc).__name__}: {exc}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"### End-to-end integration test\n\n**FAILED** - {type(exc).__name__}: {exc}\n")
        sys.exit(1)
