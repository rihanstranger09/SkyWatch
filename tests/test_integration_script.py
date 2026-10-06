"""Tests for the CI integration script (`tests/generate_and_upload_test.py`).

Stage 4 of the workflow is the only part of the pipeline that can fail *after* a
successful deploy, so its logic is exercised here with mocked AWS: the upload is
intercepted, the Lambda worker is simulated synchronously, and the script's own
polling, verification and status-snapshot writing are asserted end to end.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

import generate_and_upload_test as itest
from src import handler

REGION = "ap-south-1"
RAW_BUCKET = "skywatch-isr-collections-000000000000"
PROCESSED_BUCKET = "skywatch-isr-products-000000000000"
TABLE_NAME = "CollectionMetadata"


@pytest.fixture()
def aws(monkeypatch):
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
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("AWS_REGION", REGION)
        monkeypatch.setenv("OUTPUT_BUCKET", PROCESSED_BUCKET)
        monkeypatch.setenv("METADATA_TABLE", TABLE_NAME)
        monkeypatch.setenv("TMP_DIR", "/tmp")
        handler._reset_clients()
        yield SimpleNamespace(s3=s3, table=table)
        handler._reset_clients()


def _install_simulated_worker(aws, monkeypatch, key: str = itest.DEFAULT_KEY, message_id: str = "ci-worker-1"):
    """Make the first DynamoDB read invoke the Lambda, emulating the async worker."""
    state = {"triggered": False}

    class SimulatingTable:
        """Delegates to the real (moto) table; only the first read triggers the worker."""

        def __init__(self, real):
            self._real = real

        def get_item(self, **kwargs):
            if not state["triggered"]:
                state["triggered"] = True
                size = aws.s3.head_object(Bucket=RAW_BUCKET, Key=key)["ContentLength"]
                inner = {
                    "Records": [
                        {
                            "eventSource": "aws:s3",
                            "eventName": "ObjectCreated:Put",
                            "s3": {"bucket": {"name": RAW_BUCKET}, "object": {"key": key, "size": size}},
                        }
                    ]
                }
                event = {"Records": [{"messageId": message_id, "eventSource": "aws:sqs", "body": json.dumps(inner)}]}
                handler.lambda_handler(event, SimpleNamespace(aws_request_id=uuid.uuid4().hex))
            return self._real.get_item(**kwargs)

        def __getattr__(self, item):
            # put_item / query / everything else goes straight to the real table
            # (the handler resolves its table through the same patched boto3 module).
            return getattr(self._real, item)

    class SimulatingResource:
        def Table(self, name):  # noqa: N802 - mirrors the boto3 API
            return SimulatingTable(aws.table)

    monkeypatch.setattr(itest.boto3, "resource", lambda *args, **kwargs: SimulatingResource())
    return state


def test_script_verifies_pipeline_and_writes_snapshot(aws, tmp_path, monkeypatch):
    _install_simulated_worker(aws, monkeypatch)
    # The snapshot is tagged with where it came from, so drop the runner's own
    # GITHUB_ACTIONS variable: on a GitHub runner the script would (correctly)
    # write "github-actions" and this assertion used to fail there. The CI label
    # itself is covered by test_script_tags_snapshots_from_ci below.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    status_path = tmp_path / "pipeline-status.json"

    exit_code = itest.main(
        [
            "--bucket", RAW_BUCKET,
            "--region", REGION,
            "--processed-bucket", PROCESSED_BUCKET,
            "--table", TABLE_NAME,
            "--status-file", str(status_path),
            "--width", "64",
            "--height", "64",
            "--timeout", "20",
            "--poll-interval", "0.01",
        ]
    )

    assert exit_code == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["result"]["status"] == "SUCCEEDED"
    # Regression guard: DynamoDB returns Decimal, which json.dumps cannot serialise.
    assert isinstance(status["result"]["ndvi"]["mean"], float)
    assert isinstance(status["image"]["sizeBytes"], int)
    assert status["source"] == "local"
    assert [stage["name"] for stage in status["stages"]] == [
        "Upload", "S3 notification", "Lambda (container)", "S3 + DynamoDB write", "Verification"
    ]
    assert all(check["ok"] for check in status["checks"])
    assert status["image"]["width"] == 64 and status["image"]["bands"] == 4


def test_script_tags_snapshots_from_ci(aws, tmp_path, monkeypatch):
    """Stage 4 runs on a GitHub runner: the snapshot must say so, and carry the run context."""
    _install_simulated_worker(aws, monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "7")
    monkeypatch.setenv("GITHUB_SHA", "cafebabe")
    monkeypatch.setenv("GITHUB_REPOSITORY", "rihanstranger09/SkyWatch")
    status_path = tmp_path / "ci-status.json"

    exit_code = itest.main(
        [
            "--bucket", RAW_BUCKET,
            "--region", REGION,
            "--processed-bucket", PROCESSED_BUCKET,
            "--table", TABLE_NAME,
            "--status-file", str(status_path),
            "--width", "64",
            "--height", "64",
            "--timeout", "20",
            "--poll-interval", "0.01",
        ]
    )

    assert exit_code == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["source"] == "github-actions"
    assert status["github"]["runNumber"] == "7"
    assert status["github"]["sha"] == "cafebab"  # abbreviated to the 7-char form GitHub uses
    assert status["github"]["repository"] == "rihanstranger09/SkyWatch"


def test_script_fails_fast_when_worker_reports_failure(aws, tmp_path, monkeypatch):
    # The worker writes a FAILED record (e.g. a 3-band tile with no NIR band) instead of a COG.
    def failing_worker(event, context=None):
        aws.table.put_item(
            Item={
                "ImageId": "ci-test-raster",
                "Status": "FAILED",
                "ErrorMessage": "expected at least 4 bands, found 3",
                "UpdatedAt": "2026-01-01T00:00:00+00:00",
            }
        )
        return {"batchItemFailures": []}

    monkeypatch.setattr(handler, "lambda_handler", failing_worker)
    _install_simulated_worker(aws, monkeypatch, message_id="ci-worker-bad")

    with pytest.raises(RuntimeError, match="Lambda reported FAILED"):
        itest.main(
            [
                "--bucket", RAW_BUCKET,
                "--region", REGION,
                "--table", TABLE_NAME,
                "--status-file", str(tmp_path / "status.json"),
                "--timeout", "20",
                "--poll-interval", "0.01",
            ]
        )


def test_script_times_out_when_nothing_processes_the_tile(aws, tmp_path, monkeypatch):
    # No worker installed: the DynamoDB record never appears.
    with pytest.raises(TimeoutError, match="timed out"):
        itest.main(
            [
                "--bucket", RAW_BUCKET,
                "--region", REGION,
                "--table", TABLE_NAME,
                "--status-file", str(tmp_path / "status.json"),
                "--timeout", "0.2",
                "--poll-interval", "0.05",
            ]
        )
