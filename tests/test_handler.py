"""End-to-end worker tests.

S3 -> Lambda -> S3 + DynamoDB, entirely in memory thanks to ``moto``: no AWS
account, no credentials and no network. This is the layer that proves the
container image's logic actually works before it ever reaches ECR.
"""

from __future__ import annotations

import json
import time
import uuid
from io import BytesIO
from types import SimpleNamespace

import boto3
import numpy as np
import pytest
import rasterio

from moto import mock_aws

from src import handler
from src.indices import compute_ndvi
from synth import build_scene, make_geotiff_bytes

REGION = "ap-south-1"
RAW_BUCKET = "satellite-drone-raw-000000000000"
PROCESSED_BUCKET = "satellite-drone-processed-000000000000"
TABLE_NAME = "ImageryMetadata"


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        aws_request_id=uuid.uuid4().hex,
        function_name="geospatial-processor",
        memory_limit_in_mb="1536",
        get_remaining_time_in_millis=lambda: 180_000,
    )


def _sqs_event(bucket: str, key: str, size: int, message_id: str = "msg-1") -> dict:
    """Build the exact envelope an S3 -> SQS -> Lambda delivery produces."""
    inner = {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": bucket}, "object": {"key": key, "size": size}},
            }
        ]
    }
    return {"Records": [{"messageId": message_id, "eventSource": "aws:sqs", "body": json.dumps(inner)}]}


def _s3_event(bucket: str, key: str, size: int) -> dict:
    """Bare S3 event (``sam local invoke`` style)."""
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": bucket}, "object": {"key": key, "size": size}},
            }
        ]
    }


@pytest.fixture()
def aws(monkeypatch):
    """Mocked S3 + DynamoDB with the pipeline's env vars wired up."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (RAW_BUCKET, PROCESSED_BUCKET):
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})

        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "ImageId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "ImageId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)  # Lambda always runs with a region set
        monkeypatch.setenv("AWS_REGION", REGION)
        monkeypatch.setenv("OUTPUT_BUCKET", PROCESSED_BUCKET)
        monkeypatch.setenv("METADATA_TABLE", TABLE_NAME)
        monkeypatch.setenv("OUTPUT_PREFIX", "processed-imagery/")
        monkeypatch.setenv("PREVIEW_PREFIX", "previews/")
        monkeypatch.setenv("TMP_DIR", "/tmp")
        handler._reset_clients()
        yield SimpleNamespace(s3=s3, table=table)
        handler._reset_clients()


def test_processes_raster_end_to_end(aws):
    payload = make_geotiff_bytes()
    key = "raw-imagery/ci-test-raster.tif"
    aws.s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=payload)

    response = handler.lambda_handler(_sqs_event(RAW_BUCKET, key, len(payload)), _context())

    assert response["batchItemFailures"] == []
    item = aws.table.get_item(Key={"ImageId": "ci-test-raster"})["Item"]
    assert item["Status"] == "SUCCEEDED"
    assert item["SourceKey"] == key
    assert item["OutputBucket"] == PROCESSED_BUCKET
    assert item["OutputKey"] == "processed-imagery/ci-test-raster_ndvi_cog.tif"
    assert item["PreviewKey"] == "previews/ci-test-raster_ndvi.png"
    assert item["Width"] == 256 and item["Height"] == 256 and item["BandCount"] == 4
    assert item["Crs"] == "EPSG:4326"
    ndvi_min, ndvi_mean, ndvi_max = (float(item[k]) for k in ("NdviMin", "NdviMean", "NdviMax"))
    assert -1.0 <= ndvi_min <= ndvi_mean <= ndvi_max <= 1.0
    assert float(item["ValidPixelPct"]) == pytest.approx(100.0, abs=0.01)
    assert item["DurationMs"] >= 0
    assert item["ExpiresAt"] > int(time.time())


def test_output_cog_bands_match_ndvi_maths(aws):
    payload = make_geotiff_bytes(seed=11)
    key = "raw-imagery/seed-11.tif"
    aws.s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=payload)
    handler.lambda_handler(_sqs_event(RAW_BUCKET, key, len(payload), message_id="m2"), _context())

    item = aws.table.get_item(Key={"ImageId": "seed-11"})["Item"]
    body = aws.s3.get_object(Bucket=PROCESSED_BUCKET, Key=item["OutputKey"])["Body"].read()

    with rasterio.open(BytesIO(body)) as src:
        assert src.count == 2
        assert src.dtypes[0] == "float32"
        assert src.crs.to_string() == "EPSG:4326"
        written = src.read(1, masked=True)

    scene = build_scene(seed=11)
    reference = compute_ndvi(scene["red"], scene["nir"])
    assert np.isclose(float(np.ma.mean(written)), float(reference.mean()), atol=2e-3)
    assert np.isclose(float(np.ma.max(written)), float(reference.max()), atol=2e-3)


def test_normalises_uint16_rasters_using_scale_metadata(aws):
    payload = make_geotiff_bytes(dtype="uint16", nodata=0, scale=0.0001, seed=3)
    key = "raw-imagery/uint16-scene.tif"
    aws.s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=payload)

    handler.lambda_handler(_sqs_event(RAW_BUCKET, key, len(payload), message_id="m3"), _context())

    item = aws.table.get_item(Key={"ImageId": "uint16-scene"})["Item"]
    assert item["Status"] == "SUCCEEDED"
    scene = build_scene(seed=3)
    reference = compute_ndvi(scene["red"], scene["nir"])
    assert float(item["NdviMean"]) == pytest.approx(float(reference.mean()), abs=5e-3)


def test_accepts_bare_s3_event_for_local_invocation(aws):
    payload = make_geotiff_bytes(width=64, height=64)
    key = "local-invoke.tif"
    aws.s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=payload)

    response = handler.lambda_handler(_s3_event(RAW_BUCKET, key, len(payload)), _context())

    assert response["batchItemFailures"] == []
    assert aws.table.get_item(Key={"ImageId": "local-invoke"})["Item"]["Status"] == "SUCCEEDED"


def test_corrupt_object_fails_and_reports_batch_item_failure(aws):
    key = "raw-imagery/broken.tif"
    aws.s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=b"this is definitely not a GeoTIFF")

    response = handler.lambda_handler(_sqs_event(RAW_BUCKET, key, 34, message_id="bad-1"), _context())

    assert response["batchItemFailures"] == [{"itemIdentifier": "bad-1"}]
    item = aws.table.get_item(Key={"ImageId": "broken"})["Item"]
    assert item["Status"] == "FAILED"
    assert item["ErrorMessage"]
    assert item["UpdatedAt"] >= item["CreatedAt"]


def test_missing_nir_band_fails_cleanly(aws):
    payload = make_geotiff_bytes(bands=("red", "green", "blue"))
    key = "raw-imagery/three-band.tif"
    aws.s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=payload)

    response = handler.lambda_handler(_sqs_event(RAW_BUCKET, key, len(payload)), _context())

    assert response["batchItemFailures"] == [{"itemIdentifier": "msg-1"}]
    assert "expected at least 4 bands" in aws.table.get_item(Key={"ImageId": "three-band"})["Item"]["ErrorMessage"]


def test_non_geotiff_key_is_rejected(aws):
    key = "raw-imagery/photo.png"
    aws.s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=b"png")

    handler.lambda_handler(_sqs_event(RAW_BUCKET, key, 3, message_id="png-1"), _context())

    item = aws.table.get_item(Key={"ImageId": "photo"})["Item"]
    assert item["Status"] == "FAILED"
    assert "unsupported object type" in item["ErrorMessage"]


def test_reprocessing_is_idempotent(aws):
    payload = make_geotiff_bytes()
    key = "raw-imagery/retry-me.tif"
    aws.s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=payload)

    handler.lambda_handler(_sqs_event(RAW_BUCKET, key, len(payload), message_id="a"), _context())
    first = aws.table.get_item(Key={"ImageId": "retry-me"})["Item"]

    handler.lambda_handler(_sqs_event(RAW_BUCKET, key, len(payload), message_id="b"), _context())  # SQS redelivery
    second = aws.table.get_item(Key={"ImageId": "retry-me"})["Item"]

    assert second["Status"] == "SUCCEEDED"
    assert second["UpdatedAt"] == first["UpdatedAt"]
    assert second["DurationMs"] == first["DurationMs"]


def test_batch_reports_only_failed_messages(aws):
    good = make_geotiff_bytes(width=64, height=64)
    aws.s3.put_object(Bucket=RAW_BUCKET, Key="ok.tif", Body=good)
    aws.s3.put_object(Bucket=RAW_BUCKET, Key="bad.tif", Body=b"nope")

    event = {"Records": _sqs_event(RAW_BUCKET, "ok.tif", len(good), "ok-1")["Records"] + _sqs_event(RAW_BUCKET, "bad.tif", 4, "bad-1")["Records"]}
    response = handler.lambda_handler(event, _context())

    assert response["batchItemFailures"] == [{"itemIdentifier": "bad-1"}]
    assert aws.table.get_item(Key={"ImageId": "ok"})["Item"]["Status"] == "SUCCEEDED"
    assert aws.table.get_item(Key={"ImageId": "bad"})["Item"]["Status"] == "FAILED"
