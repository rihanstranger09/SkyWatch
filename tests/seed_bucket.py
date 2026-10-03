#!/usr/bin/env python3
"""Seed the raw imagery bucket with a handful of synthetic scenes.

Useful for populating the dashboard / demoing the pipeline without satellite data:

    python tests/seed_bucket.py --bucket satellite-drone-raw-<account-id> --region ap-south-1 --count 3

Each scene is uploaded under ``raw-imagery/`` with a distinct seed, which triggers
the S3 -> SQS -> Lambda path exactly like a real GeoTIFF drop.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import make_geotiff_bytes  # noqa: E402


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="raw imagery bucket")
    parser.add_argument("--region", required=True)
    parser.add_argument("--count", type=int, default=3, help="number of scenes to upload")
    parser.add_argument("--prefix", default="raw-imagery/", help="key prefix")
    parser.add_argument("--scene-prefix", default="demo-scene", help="object name stem")
    parser.add_argument("--size", type=int, default=256, help="raster width/height in pixels")
    parser.add_argument("--table", default="ImageryMetadata")
    parser.add_argument("--wait", action="store_true", help="poll DynamoDB until every scene is processed")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    s3 = boto3.client("s3", region_name=args.region)
    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)

    image_ids = []
    for index in range(1, args.count + 1):
        stem = f"{args.scene_prefix}-{index:02d}"
        key = f"{args.prefix}{stem}.tif"
        body = make_geotiff_bytes(width=args.size, height=args.size, seed=100 + index)
        s3.put_object(Bucket=args.bucket, Key=key, Body=body, ContentType="image/tiff")
        image_ids.append(stem)
        print(f"uploaded s3://{args.bucket}/{key} ({len(body) / 1024:.1f} KiB)")
        time.sleep(0.2)  # gentle pacing keeps S3 notification ordering obvious

    if not args.wait:
        print(f"queued {len(image_ids)} scenes -> watch the dashboard or the DynamoDB table")
        return 0

    print("waiting for the async worker...")
    deadline = time.time() + 240
    pending = set(image_ids)
    while pending and time.time() < deadline:
        for image_id in sorted(pending):
            item = table.get_item(Key={"ImageId": image_id}).get("Item")
            if item and item.get("Status") == "SUCCEEDED":
                print(f"  {image_id}: SUCCEEDED (NDVI mean {item.get('NdviMean')})")
                pending.discard(image_id)
            elif item and item.get("Status") == "FAILED":
                print(f"  {image_id}: FAILED -> {item.get('ErrorMessage')}")
                pending.discard(image_id)
        if pending:
            time.sleep(4)

    if pending:
        print(f"WARNING: still pending after timeout: {sorted(pending)}")
        return 1
    print("all scenes processed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
