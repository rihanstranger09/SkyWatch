# 🛰️ Satellite & Drone Imagery Pipeline — Event-Driven, ₹0/Month, Fully Automated

An end-to-end, **event-driven geospatial processing platform on AWS** that ingests satellite/drone GeoTIFFs,
computes vegetation (NDVI) and water (NDWI) indices inside a containerised AWS Lambda, writes Cloud-Optimized
GeoTIFF outputs back to S3 and a metadata record to DynamoDB — then **proves it works on every push** with a
GitHub Actions pipeline that builds the image, deploys the stack with AWS SAM, and runs a synthetic-raster
integration test.

[![Geospatial Pipeline CI/CD](https://github.com/rihanstranger09/SkyWatch/actions/workflows/deploy-pipeline.yml/badge.svg)](https://github.com/rihanstranger09/SkyWatch/actions/workflows/deploy-pipeline.yml)
[![Frontend Quality](https://github.com/rihanstranger09/SkyWatch/actions/workflows/frontend-quality.yml/badge.svg)](https://github.com/rihanstranger09/SkyWatch/actions/workflows/frontend-quality.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-2ea44f.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11-3776ab.svg)
![Runtime](https://img.shields.io/badge/Lambda-container%20image-ff9900.svg)
![Cost](https://img.shields.io/badge/monthly%20cost-%E2%82%B90-22c55e.svg)

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Quickstart (local, no AWS account)](#quickstart-local-no-aws-account)
- [Deploying to AWS](#deploying-to-aws)
- [CI/CD on GitHub Actions](#cicd-on-github-actions)
- [Required GitHub secrets](#required-github-secrets)
- [The ₹0 budget maths](#the-₹0-budget-maths)
- [Data contract](#data-contract)
- [Testing strategy](#testing-strategy)
- [Live dashboard (GitHub Pages)](#live-dashboard-github-pages)
- [Troubleshooting](#troubleshooting)
- [Security notes & hardening path](#security-notes--hardening-path)

---

## What it does

| Step | Trigger | Result |
| --- | --- | --- |
| 1 | A `.tif` / `.tiff` object lands in `s3://satellite-drone-raw-<account>/` | S3 publishes an `ObjectCreated:*` notification to SQS |
| 2 | `imagery-processing-queue` delivers the event to the Lambda | Container Lambda (GDAL + rasterio baked in) starts |
| 3 | Lambda reads Red / Green / NIR bands | `NDVI = (NIR − RED) / (NIR + RED)`, `NDWI = (GREEN − NIR) / (GREEN + NIR)` |
| 4 | Lambda writes outputs | `processed-imagery/<id>_ndvi_cog.tif` (COG, NDVI + NDWI bands) and `previews/<id>_ndvi.png` |
| 5 | Lambda upserts metadata | DynamoDB `ImageryMetadata` record: bounds, CRS, index statistics, sizes, timings |
| 6 | Failures | Retried 3× by SQS, then parked in `imagery-processing-dlq`; CloudWatch alarm fires |

Two things make this repo useful rather than a toy:

- **The maths is decoupled from the cloud.** `src/indices.py` is pure NumPy — so NDVI/NDWI are unit-tested
  in ~50 ms with no AWS, no GDAL, no credentials.
- **The pipeline is verified, not assumed.** The GitHub Actions integration test uploads a synthetic
  4-band GeoTIFF, polls DynamoDB for the asynchronous result, re-opens the produced COG with rasterio and
  publishes a run snapshot the dashboard renders.

---

## Architecture

```
                       ┌─────────────────────────── GitHub Actions (ubuntu-latest) ───────────────────────────┐
                       │                                                                                      │
[ git push / PR ]─────▶│  Stage 1  flake8 · pytest (moto) · cfn-lint · sam validate                            │
                       │  Stage 2  sam build --use-container  ──▶  OCI image  ──▶  AWS ECR                      │
                       │  Stage 3  sam deploy  ──▶  CloudFormation stack (S3, SQS, DLQ, Lambda, DynamoDB)     │
                       │  Stage 4  synthetic GeoTIFF ──▶ S3 ──▶ SQS ──▶ Lambda ──▶ COG + DynamoDB ──▶ verify   │
                       └──────────────────────────────────────────────────────────────────────────────────────┘

  Runtime (ap-south-1)
  ┌────────────────┐   ObjectCreated:*.tif    ┌──────────────────────────┐   batch of 1   ┌───────────────────────────┐
  │ S3 raw bucket  │─────────────────────────▶│ SQS imagery-processing-  │───────────────▶│ Lambda (container image)  │
  │ raw-imagery/   │                          │ queue  (visibility 360s) │                │  rasterio + GDAL + numpy  │
  └────────────────┘                          └───────────┬──────────────┘                └───────┬───────────────────┘
                                                          │ 3 failed receives                          │
                                                          ▼                                            │ COG + PNG
                                              ┌────────────────────────┐                                 ▼
                                              │ SQS dead-letter queue  │                     ┌──────────────────────────┐
                                              │ + CloudWatch alarm     │                     │ S3 processed bucket      │
                                              └────────────────────────┘                     │ processed-imagery/       │
                                                                                             │ previews/                │
                                                                                             └───────────┬──────────────┘
                                                                                                         │ metadata
                                                                                                         ▼
                                                                                             ┌──────────────────────────┐
                                                                                             │ DynamoDB ImageryMetadata │
                                                                                             │ PK: ImageId · TTL 30d    │
                                                                                             └──────────────────────────┘
```

**Why SQS in the middle?** S3 → Lambda direct invocation has no retry buffer and no dead-letter story; SQS adds
buffering, a visibility timeout matched to the Lambda timeout, automatic retry, and a DLQ you can inspect. It also
decouples ingest bursts (a drone flight uploading 400 tiles) from concurrency.

---

## Repository layout

```
.
├── .github/
│   └── workflows/
│       ├── deploy-pipeline.yml        # Stage 1-4 CI/CD: test → image → SAM deploy → e2e verify
│       ├── frontend-quality.yml       # self-containment + fallback-data checks for the dashboard
│       └── publish-dashboard.yml      # republishes Pages when only frontend/ changes
├── frontend/
│   ├── index.html                     # fully animated ops console (single file, zero dependencies)
│   ├── pipeline-status.json           # committed fallback snapshot for the dashboard
│   └── publish_status.py              # builds the GitHub Pages artefact (_site/)
├── src/
│   ├── Dockerfile                     # Lambda container runtime (rasterio/GDAL via manylinux wheels)
│   ├── handler.py                     # SQS → rasterio → NDVI/NDWI → COG + DynamoDB worker
│   ├── indices.py                     # pure-NumPy spectral maths (unit-testable without AWS)
│   └── requirements.txt               # pinned runtime dependencies
├── tests/
│   ├── test_processor.py              # spectral index unit tests
│   ├── test_handler.py                # moto-mocked S3 → Lambda → DynamoDB end-to-end tests
│   ├── synth.py                       # synthetic Bengaluru GeoTIFF generator (shared fixture)
│   ├── generate_and_upload_test.py    # the CI integration test (upload, poll, verify, report)
│   ├── test_integration_script.py     # tests for the CI verifier itself
│   ├── test_frontend_build.py         # tests for the dashboard build tooling
│   ├── seed_bucket.py                 # seed demo scenes into a deployed bucket
│   ├── conftest.py                    # sys.path bootstrap
│   └── requirements-dev.txt           # test/lint dependencies
├── events/
│   └── sqs-trigger.json               # sample event for `sam local invoke`
├── scripts/
│   └── local_e2e.py                   # whole pipeline on mocked AWS + status snapshot
├── template.yaml                      # AWS SAM: S3, SQS, DLQ, DynamoDB, Lambda, alarm, outputs
├── samconfig.toml                     # zero-configuration `sam deploy` (ap-south-1 by default)
├── Makefile                           # make lint / test / local / deploy / e2e
├── LICENSE
└── README.md
```

---

## Quickstart (local, no AWS account)

```bash
git clone https://github.com/rihanstranger09/SkyWatch.git
cd satellite-drone-pipeline

python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
make install                                           # test + lint dependencies

make test      # 40 unit / integration tests, all mocked with moto
make lint      # flake8 --max-line-length=120
make local     # runs the FULL pipeline against mocked AWS
```

`make local` prints the four stages and writes artefacts you can actually look at:

```
▌ Stage 1/4  synthesise GeoTIFF (Bengaluru, EPSG:4326, 4 bands)
▌ Stage 2/4  SQS-delivered event -> container Lambda (moto-backed)
▌ Stage 3/4  verify outputs
   COG      : processed-imagery/local-demo_ndvi_cog.tif (…)
   NDVI     : mean=0.4… min=-0.5… max=0.9… valid=100.0%
▌ Stage 4/4  write dashboard snapshot
artifacts/local-demo-ndvi-cog.tif
artifacts/local-demo-ndvi-preview.png        # colour-mapped NDVI preview
artifacts/pipeline-status.json               # feed it to the dashboard
```

Preview the dashboard locally: open `frontend/index.html` and drop `artifacts/pipeline-status.json` onto it
(or copy it to `frontend/pipeline-status.json` and reload).

---

## Deploying to AWS

Prerequisites: an AWS account, [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html),
Docker (for `sam build --use-container`), and credentials for a user/role that can create S3, SQS, DynamoDB,
Lambda, IAM and CloudFormation resources.

```bash
sam build --use-container
sam deploy --guided          # first time only; samconfig.toml then holds the answers
```

Or, non-interactively (same flags the CI uses):

```bash
sam deploy --no-confirm-changeset --no-fail-on-empty-changeset \
  --stack-name satellite-drone-pipeline \
  --resolve-s3 --resolve-image-repos \
  --capabilities CAPABILITY_IAM --region ap-south-1
```

Then trigger it for real:

```bash
RAW_BUCKET=$(aws cloudformation describe-stacks --stack-name satellite-drone-pipeline \
  --query "Stacks[0].Outputs[?OutputKey=='RawImageryBucketName'].OutputValue" --output text)

# option A: the synthetic scene used by CI
python tests/generate_and_upload_test.py --bucket "$RAW_BUCKET" --region ap-south-1

# option B: three demo scenes, then wait for them
make seed

# option C: a real GeoTIFF you already have
aws s3 cp scene.tif "s3://$RAW_BUCKET/raw-imagery/scene.tif"
```

Check the metadata that landed:

```bash
aws dynamodb scan --table-name ImageryMetadata --max-items 5
aws s3 ls "s3://$(aws cloudformation describe-stacks --stack-name satellite-drone-pipeline \
  --query "Stacks[0].Outputs[?OutputKey=='ProcessedImageryBucketName'].OutputValue" --output text)/processed-imagery/"
```

To tear everything down: `sam delete --stack-name satellite-drone-pipeline`.

---

## CI/CD on GitHub Actions

`.github/workflows/deploy-pipeline.yml` runs four stages:

| Stage | Job | What it proves | Credentials needed |
| --- | --- | --- | --- |
| 1 | `test` | flake8 clean, 40 tests green (moto-mocked S3/DynamoDB), `cfn-lint` + `sam validate --lint` pass | none |
| 2 + 3 | `deploy` | the OCI image builds, pushes to ECR and `sam deploy` converges the CloudFormation stack | AWS secrets |
| 4 | `integration-test` | a real synthetic GeoTIFF travels S3 → SQS → Lambda → S3 + DynamoDB and the output COG is re-read | AWS secrets |

Behaviour details worth knowing:

- **PR runs stop after Stage 1** — no AWS credentials are exposed to pull requests, and nothing deploys from a fork.
- **Only pushes to `main` deploy**; `frontend/**` and `*.md` changes skip the deploy pipeline entirely
  (the dashboard has its own fast workflow).
- **Manual runs** (`workflow_dispatch`) can toggle the integration test on/off; they still deploy.
- **Concurrency** is keyed per ref, so two pushes to `main` queue up instead of fighting over the stack.
- **Job summaries** — each stage writes a Markdown table into the run summary (resources, NDVI stats, dashboard URL).
- PowerShell-free, dependency-free: everything runs on `ubuntu-latest` with `actions/checkout@v4`,
  `actions/setup-python@v5`, `aws-actions/configure-aws-credentials@v4`, `aws-actions/setup-sam@v2`,
  `actions/upload-artifact@v4`, `actions/deploy-pages@v4`.

Stage 4 fails the workflow loudly (non-zero exit) when the asynchronous pipeline does not complete, with the
troubleshooting checklist printed in the log — a silent "green" run is worse than a red one.

---

## Required GitHub secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Example | Purpose | Zero-cost |
| --- | --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | `AKIA…` | IAM user access key with deployment rights | ✅ |
| `AWS_SECRET_ACCESS_KEY` | `wJalr…` | IAM user secret key | ✅ |
| `AWS_REGION` | `ap-south-1` | Region for SAM deploy and the integration test | ✅ |
| `AWS_ACCOUNT_ID` | `123456789012` | Optional — used as a fallback for the ECR/bucket naming narrative; the workflow resolves the account from STS automatically | ✅ |

Minimum IAM permissions for the deployer user (tighten with a permissions boundary in production):
`cloudformation:*`, `s3:*` (SAM artefact + imagery buckets), `sqs:*`, `dynamodb:*`, `lambda:*`, `ecr:*`,
`iam:PassRole/CreateRole/AttachRolePolicy/GetRole/DeleteRole`, `logs:*`, `cloudwatch:*`, `sts:GetCallerIdentity`.

> **OIDC instead of long-lived keys (recommended).** Replace the `Configure AWS Credentials` step with
> `role-to-assume: arn:aws:iam::<account>:role/github-actions-deploy` and add a GitHub OIDC identity provider
> in IAM. No static keys exist, nothing to rotate, and the trust policy can be scoped to this repository and
> branch. The workflow keeps `permissions: contents: read` either way.

---

## The ₹0 budget maths

| Service | Free tier (per month) | This pipeline's usage |
| --- | --- | --- |
| GitHub Actions | 2,000 runner minutes (Linux, free plan) | ~8–10 min per full run ⇒ **~150+ runs/month free** |
| Lambda (container) | 400,000 GB-s + 1M requests | 256×256 tile ≈ 1.5 s × 1.5 GB ≈ **2.3 GB-s per tile** ⇒ ~170,000 tiles |
| S3 | 5 GB storage, 20k GET, 2k PUT | 3-day raw retention + 7-day processed retention keeps it in the low MB |
| SQS | 1M requests | 1 send + 1 receive + 1 delete per tile |
| DynamoDB | 25 GB + 25 WCU/25 RCU (on-demand free tier) | One tiny item per tile, TTL-cleaned after 30 days |
| ECR | 500 MB private storage (free tier) | One image (~450 MB with GDAL wheels) — pruned by `sam deploy` on each release |
| CloudWatch | 10 alarms + 5 GB logs | 1 DLQ alarm, 14-day log retention (configured via `LOG_LEVEL`/console) |

Cost hygiene baked into the template:

- raw bucket expires objects after **3 days** (`DeleteAfter3Days`), processed outputs after **7 days**
  (`ProcessedRetentionDays` parameter),
- `AbortIncompleteMultipartUpload` kills half-uploaded tiles,
- DynamoDB items carry `ExpiresAt` and the table has **TTL enabled**,
- `PAY_PER_REQUEST` everywhere — no idle capacity charges,
- Lambda `MaximumConcurrency: 5` on the SQS event source caps runaway fan-out (and thereby cost),
- `EphemeralStorage: 2048 MB` is free up to the 512 MB baseline + 2 GB add-on region support.

> Turn the stack off between demos: `sam delete --stack-name satellite-drone-pipeline`. Redeploying is one
> `sam deploy` away, and the CI does it for you on the next push.

---

## Data contract

### S3 layout

| Bucket | Prefix | Contents |
| --- | --- | --- |
| `satellite-drone-raw-<account>` | any key ending `.tif` / `.tiff` | input rasters, deleted after 3 days |
| `satellite-drone-processed-<account>` | `processed-imagery/` | `<image-id>_ndvi_cog.tif` — COG, band 1 NDVI, band 2 NDWI, float32, DEFLATE |
| `satellite-drone-processed-<account>` | `previews/` | `<image-id>_ndvi.png` — colour-mapped NDVI thumbnail |

`<image-id>` is the source filename without extension (`raw-imagery/bengaluru_2026_03.tif` → `bengaluru_2026_03`).

### Band contract

| Env var | Default | Meaning |
| --- | --- | --- |
| `RED_BAND` | `1` | Red band index (1-based) |
| `GREEN_BAND` | `2` | Green band index |
| `NIR_BAND` | `4` | Near-infrared band index (Sentinel-2-like ordering) |

Inputs are normalised to reflectance: explicit `scales`/`offsets` win, otherwise integer dtypes are divided by
their `dtype_max`, and nodata pixels become `NaN` so they never distort statistics.

### DynamoDB `ImageryMetadata`

| Attribute | Type | Notes |
| --- | --- | --- |
| `ImageId` | S | **Partition key** |
| `Status` | S | `PROCESSING` → `SUCCEEDED` / `FAILED` |
| `SourceBucket`, `SourceKey`, `SourceSizeBytes` | S/N | Where the tile came from |
| `OutputBucket`, `OutputKey`, `OutputSizeBytes`, `PreviewKey` | S/N | Where the results live |
| `Width`, `Height`, `BandCount`, `Crs`, `Bounds` | N/S/L | Raster profile |
| `NdviMean`, `NdviMin`, `NdviMax`, `NdviStd`, `ValidPixelPct` | N | Index statistics over valid pixels only |
| `NdwiMean`, `NdwiMin`, `NdwiMax` | N | Water index statistics |
| `DurationMs`, `CreatedAt`, `UpdatedAt`, `ExpiresAt` | N/S | Timings, TTL (30 days) |

Records are idempotent: a redelivered SQS message for an already-`SUCCEEDED` tile short-circuits instead of
recomputing, which keeps retries cheap.

---

## Testing strategy

| Layer | File | What it covers |
| --- | --- | --- |
| Pure maths | `tests/test_processor.py` | NDVI/NDWI ranges, zero-division guards, nodata propagation, uint16 scaling, preview ramp |
| AWS integration (mocked) | `tests/test_handler.py` | full S3 → Lambda → S3 + DynamoDB path with `moto`, COG band correctness vs. recomputed NDVI, uint16 + scale metadata, corrupt/3-band rasters, batch item failures, idempotency |
| Real AWS (post-deploy) | `tests/generate_and_upload_test.py` | the asynchronous production path, plus COG header re-read and status snapshot |
| Infrastructure | `cfn-lint`, `sam validate --lint` | template validity and best practices |
| Frontend build | `tests/test_frontend_build.py` | status injection is surgical, the publisher degrades to the committed fallback, embedded assets decode to a square float32 grid |
| Frontend (headless) | `frontend-quality.yml` | dashboard is self-contained (no external scripts/styles/data URIs only), the fallback snapshot parses, the Pages artefact boots |

```bash
pytest tests/ -k ndvi -q                    # just the index maths
pytest tests/test_handler.py -vv            # the mocked AWS path
python scripts/local_e2e.py                 # the whole thing, locally
```

---

## Live dashboard (GitHub Pages)

`frontend/index.html` is a single-file ops console built on a "sky & sheet" editorial design (adapted from a
supplied layout reference, which is not redistributed in this repository), wired to this repository's real
pipeline:

- **seven pipeline stages** — `Upload · S3 event · SQS · Validate · NDVI + COG · DynamoDB · Publish` — with a
  streaming worker log, a progress meter and per-stage durations taken from the run snapshot;
- **a living sky** — six motions run at once: a huge blurred veil of colour turns behind the page (150 s per
  revolution), the three depth layers each pan and breathe on their own clock (72 s / 104 s / 150 s), balloons
  travel up to 9 vw and breathe to 1.12× scale with a slow rotation, cloud bands slide across the sheet in both
  directions, twelve motes rise through the frame on 24–52 s cycles, and in-sheet glows float. Scroll and pointer
  movement parallax the layers on top of all of that (the ambient pan and the parallax compose through `--px` /
  `--py`, so neither clobbers the other), and every one of these stands still for visitors who request
  `prefers-reduced-motion`;
- **the scene map** — Leaflet when the CDN is reachable, and the built-in SVG engine (coastline, range rings,
  scale bar, pins, click-to-inspect) when it is not, so the console works fully offline;
- **the result explorer** — the NDVI render produced by `src/indices.py` is embedded as a data URI and painted
  into the overlay canvas with an opacity slider, alongside the scene's real statistics and a STAC item;
- the architecture table (local path → AWS service), the cost meter, and the run's checks.

It is **genuinely self-contained**: the NDVI render and the scene metadata (produced by the pipeline's own
maths) are embedded, so the page renders identically from a downloaded file, behind a corporate proxy, inside a
sandboxed iframe, or on GitHub Pages — no build step and no required network. The two external requests that
remain (Google Fonts, Leaflet) are progressive enhancements that fail silently into the system font stack and
the SVG map engine.

The run snapshot comes from `frontend/pipeline-status.json` (committed fallback), and Stage 4 injects the
real snapshot at publish time:

```bash
python frontend/make_ndvi_preview.py   # re-render assets from the pipeline's own maths
python frontend/embed_assets.py        # inline them into index.html (+ fallback snapshot)
python frontend/publish_status.py      # build the _site/ artefact for GitHub Pages
```


Enable it once: **Settings → Pages → Source: GitHub Actions**. After that:

- the `integration-test` job publishes the freshly generated snapshot,
- `publish-dashboard.yml` republishes on frontend-only changes.

Both use `actions/upload-pages-artifact@v3` + `actions/deploy-pages@v4`; the deploy step is
`continue-on-error` so a Pages misconfiguration can never fail the actual pipeline run.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Integration test times out; DynamoDB has no record | Lambda never ran | `aws sqs get-queue-attributes --queue-url <url> --attribute-names ApproximateNumberOfMessages`; check the S3 notification points at `imagery-processing-queue` and the raw bucket name matches `satellite-drone-raw-<account>` |
| Record says `FAILED: expected at least 4 bands` | Your raster has no NIR band | Set `NIR_BAND` / `RED_BAND` / `GREEN_BAND` env vars on the function to match your sensor |
| `AccessDenied` on `sam deploy` | Deployer IAM policy too narrow, or the ECR repo name is already taken | Add `ecr:*` + `iam:PassRole`, and let `--resolve-image-repos` create `satellite-drone-pipeline-*` repos |
| Image pull failure during `sam build --use-container` | Docker not running on the runner / local machine | Start Docker; the base image is public ECR (`public.ecr.aws/lambda/python:3.11`) |
| `ResourceConflictException` on Lambda update | A previous deploy is still settling | Re-run the job; SAM retries the change set |
| DLQ alarm in `ALARM` | Poison message (corrupt tile, wrong CRS) | Read the item's `ErrorMessage` in DynamoDB, fix the raster, re-upload |
| COG preview is all one colour | Input bands are 8-bit imagery rather than reflectance | Check `NdviMin`/`NdviMax` — normalisation uses `scales` when present, `dtype_max` otherwise |

---

## Security notes & hardening path

Implemented here: private buckets with public access fully blocked, SSE on S3/SQS/DynamoDB, least-privilege
Lambda policies (`S3ReadPolicy` for the raw bucket, `S3CrudPolicy` for the output bucket, `DynamoDBCrudPolicy`
for one table), scoped SQS resource policy so only *this* bucket can enqueue, no credentials in the repository,
`permissions: contents: read` on the workflows.

Next steps for production: OIDC instead of static keys, a VPC + S3 gateway endpoint for the Lambda, KMS CMKs
instead of AWS-managed keys, SQS event-source `FilterCriteria` to ignore non-imagery keys, X-Ray tracing,
and moving the integration test behind a `production` environment approval.

---

## Roadmap

- [ ] Batch large rasters with `rasterio.windows` + `STAC` metadata output
- [ ] Tile-server friendly pyramid export (`rio-cogeo` overviews are already enabled)
- [ ] Multi-index support (NDMI, NBR for burn scars) driven by a JSON config
- [ ] CloudWatch Embedded Metric Format for per-scene NDVI dashboards
- [ ] SAM `sam local start-api` read API for the dashboard's live data path

---

MIT licensed. Built to run on the free tier; sized to scale when it needs to.
