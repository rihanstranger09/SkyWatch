# SkyWatch ISR Processing Line - developer shortcuts.
# Region/stack defaults live in samconfig.toml (ap-south-1, skywatch-isr-line).

PYTHON ?= python3
REGION ?= ap-south-1
RAW_BUCKET ?= $(shell aws cloudformation describe-stacks --stack-name skywatch-isr-line \
	--region $(REGION) --query "Stacks[0].Outputs[?OutputKey=='CollectionStoreBucketName'].OutputValue" \
	--output text 2>/dev/null)

.PHONY: help install lint test validate local frontend frontend-check build deploy e2e seed clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install dev/test dependencies
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r tests/requirements-dev.txt

lint: ## Flake8 across src, tests, scripts and frontend tooling
	$(PYTHON) -m flake8 src/ tests/ scripts/ frontend/ --max-line-length=120 --ignore=E501,W503

test: ## Unit + moto-mocked end-to-end tests with coverage
	$(PYTHON) -m pytest tests/ --cov=src --cov-report=term-missing

validate: ## cfn-lint the SAM template and validate it with the SAM CLI
	$(PYTHON) -m cfn_lint template.yaml --ignore-checks W3005,W3011 || cfn-lint template.yaml --ignore-checks W3005,W3011
	sam validate --lint

local: ## Full pipeline against mocked AWS (S3 -> Lambda -> S3 + DynamoDB)
	$(PYTHON) scripts/local_e2e.py --outdir artifacts

change-demo: ## Two epochs of the sample tile -> artefacts plus a change report
	$(PYTHON) scripts/make_change_demo.py

frontend: ## Regenerate the console assets from src/indices.py and embed them
	$(PYTHON) frontend/make_ndvi_preview.py
	$(PYTHON) frontend/embed_assets.py
	$(PYTHON) frontend/publish_status.py --site-dir _site --status frontend/pipeline-status.json

frontend-check: ## Verify the dashboard bundle, embedded assets and Pages artefact
	$(PYTHON) frontend/embed_assets.py --check
	$(PYTHON) -m pytest tests/test_frontend_build.py -q
	$(PYTHON) frontend/publish_status.py --check

build: ## SAM build with the container image
	sam build --use-container

deploy: ## Deploy the stack (guided first run: sam deploy --guided)
	sam deploy

e2e: ## Upload a synthetic GeoTIFF to the deployed stack and verify the async result
	$(PYTHON) tests/generate_and_upload_test.py --bucket "$(RAW_BUCKET)" --region $(REGION) \
		--status-file pipeline-status.json

seed: ## Drop three demo scenes into the raw bucket for the console
	$(PYTHON) tests/seed_bucket.py --bucket "$(RAW_BUCKET)" --region $(REGION) --count 3

clean: ## Remove build/test artefacts
	rm -rf .aws-sam build dist _site artifacts .pytest_cache .coverage coverage.xml
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
