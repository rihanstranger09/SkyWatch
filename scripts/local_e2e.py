#!/usr/bin/env python3
"""Local, credential-free end-to-end demo of the whole pipeline.

Runs the real Lambda handler against mocked AWS services (moto) - S3 in, NDVI +
COG out, DynamoDB metadata record - and writes the same ``pipeline-status.json``
contract the CI integration test produces, so the dashboard can be developed
offline.

    python scripts/local_e2e.py --outdir artifacts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from src import handler  # noqa: E402
from synth import BENGALURU_ORIGIN, DEFAULT_PIXEL_SIZE, jsonable, make_geotiff_bytes  # noqa: E402

REGION = "ap-south-1"
RAW_BUCKET = "satellite-drone-raw-000000000000"
PROCESSED_BUCKET = "satellite-drone-processed-000000000000"
TABLE_NAME = "ImageryMetadata"
KEY = "raw-imagery/local-demo.tif"


def banner(text: str) -> None:
    print(f"\n\033[38;5;42m▌\033[0m {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="artifacts", help="where the preview PNG + status JSON land")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    outdir = ROOT / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    os.environ.update(
        AWS_DEFAULT_REGION=REGION,
        AWS_REGION=REGION,
        OUTPUT_BUCKET=PROCESSED_BUCKET,
        METADATA_TABLE=TABLE_NAME,
        OUTPUT_PREFIX="processed-imagery/",
        PREVIEW_PREFIX="previews/",
        TMP_DIR="/tmp",
        WRITE_PREVIEW="true",
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (RAW_BUCKET, PROCESSED_BUCKET):
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
        table = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "ImageId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "ImageId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        handler._reset_clients()

        banner("Stage 1/4  synthesise GeoTIFF (Bengaluru, EPSG:4326, 4 bands)")
        started = time.time()
        raster = make_geotiff_bytes(seed=args.seed)
        s3.put_object(Bucket=RAW_BUCKET, Key=KEY, Body=raster)
        print(f"   {len(raster) / 1024:.1f} KiB queued -> s3://{RAW_BUCKET}/{KEY}")

        banner("Stage 2/4  SQS-delivered event -> container Lambda (moto-backed)")
        inner = {
            "Records": [
                {
                    "eventSource": "aws:s3",
                    "eventName": "ObjectCreated:Put",
                    "s3": {"bucket": {"name": RAW_BUCKET}, "object": {"key": KEY, "size": len(raster)}},
                }
            ]
        }
        event = {"Records": [{"messageId": uuid.uuid4().hex, "eventSource": "aws:sqs", "body": json.dumps(inner)}]}
        context = SimpleNamespace(aws_request_id=uuid.uuid4().hex, function_name="local-demo")
        response = handler.lambda_handler(event, context)
        if response["batchItemFailures"]:
            raise SystemExit(f"pipeline failed: {response['batchItemFailures']}")

        banner("Stage 3/4  verify outputs")
        item = table.get_item(Key={"ImageId": "local-demo"})["Item"]
        cog = s3.get_object(Bucket=PROCESSED_BUCKET, Key=item["OutputKey"])["Body"].read()
        preview_key = item.get("PreviewKey")
        preview = s3.get_object(Bucket=PROCESSED_BUCKET, Key=preview_key)["Body"].read() if preview_key else None
        (outdir / "local-demo-ndvi-cog.tif").write_bytes(cog)
        if preview:
            (outdir / "local-demo-ndvi-preview.png").write_bytes(preview)
        print(f"   COG      : {item['OutputKey']} ({len(cog)} bytes)")
        print(f"   preview  : {preview_key} ({len(preview) if preview else 0} bytes)")
        print(f"   NDVI     : mean={item['NdviMean']} min={item['NdviMin']} max={item['NdviMax']} valid={item['ValidPixelPct']}%")
        print(f"   duration : {item['DurationMs']} ms")

        banner("Stage 4/4  write dashboard snapshot")
        status = {
            "source": "local-demo",
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stack": "satellite-drone-pipeline (local)",
            "region": REGION,
            "github": None,
            "image": {
                "id": "local-demo",
                "key": KEY,
                "bucket": RAW_BUCKET,
                "sizeBytes": len(raster),
                "width": item["Width"],
                "height": item["Height"],
                "bands": item["BandCount"],
                "crs": item["Crs"],
                "origin": list(BENGALURU_ORIGIN),
                "pixelSize": DEFAULT_PIXEL_SIZE,
                "sceneNdviMean": None,
            },
            "result": {
                "status": item["Status"],
                "durationMs": item["DurationMs"],
                "wallClockMs": int((time.time() - started) * 1000),
                "pollAttempts": 1,
                "ndvi": {
                    "mean": item["NdviMean"],
                    "min": item["NdviMin"],
                    "max": item["NdviMax"],
                    "std": item["NdviStd"],
                    "validPixelPct": item["ValidPixelPct"],
                },
                "ndwi": {"mean": item.get("NdwiMean")},
                "outputKey": item["OutputKey"],
                "outputSizeBytes": item["OutputSizeBytes"],
                "previewKey": preview_key,
                "bounds": item["Bounds"],
            },
            "stages": [
                {"name": "Upload", "status": "success", "durationMs": None, "detail": KEY},
                {"name": "S3 notification", "status": "success", "durationMs": None, "detail": "ObjectCreated:* -> SQS"},
                {"name": "Lambda (container)", "status": "success", "durationMs": item["DurationMs"], "detail": f"NDVI mean {item['NdviMean']}"},
                {"name": "S3 + DynamoDB write", "status": "success", "durationMs": None, "detail": item["OutputKey"]},
                {"name": "Verification", "status": "success", "durationMs": None, "detail": "local"},
            ],
            "checks": [
                {"name": "Processed COG present in S3", "ok": True, "detail": f"{len(cog)} bytes"},
                {"name": "PNG preview present", "ok": bool(preview), "detail": preview_key or "disabled"},
            ],
        }
        status_path = outdir / "pipeline-status.json"
        status_path.write_text(json.dumps(jsonable(status), indent=2), encoding="utf-8")
        print(f"   wrote {status_path.relative_to(ROOT)}")
        print("   copy it to frontend/pipeline-status.json to preview the live board\n")

    banner("LOCAL END-TO-END RUN PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
