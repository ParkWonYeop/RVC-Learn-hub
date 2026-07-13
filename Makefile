PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest
RUFF ?= .venv/bin/ruff
MYPY ?= .venv/bin/mypy

.PHONY: bootstrap check test-python test-e2e test-manager-recovery-docker test-manager-full-stack-docker test-manager-secret-projection-docker test-minio-policy-docker test-redis-acl-docker test-mlflow-docker lint-python typecheck-python test-web syntax-shell

bootstrap:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt
	cd apps/web && npm ci --ignore-scripts --no-audit --no-fund

test-python:
	$(PYTEST) -m "not e2e"

test-e2e:
	$(PYTEST) tests/e2e -m e2e

test-manager-recovery-docker:
	bash tests/recovery/manager_volume_drill.sh

test-manager-full-stack-docker:
	bash tests/infra/manager_full_stack_smoke.sh

test-manager-secret-projection-docker:
	bash tests/infra/manager_secret_projection_smoke.sh

test-minio-policy-docker:
	bash tests/infra/minio_policy_scope_smoke.sh

test-redis-acl-docker:
	bash tests/infra/redis_acl_scope_smoke.sh

test-mlflow-docker:
	bash tests/infra/mlflow_nonroot_smoke.sh

lint-python:
	$(RUFF) check packages/contracts apps/api apps/worker infra/worker/runtime/qualification.py \
		infra/worker/runtime/release_readiness.py \
		infra/runtime/manager-secrets-init.py infra/runtime/maintenance-db-authz.py \
		installers/common/image_bundle.py installers/common/publish_release_bundle.py \
		tools tests/infra

typecheck-python:
	$(MYPY) packages/contracts/src apps/api/src apps/worker/src \
		infra/worker/runtime/qualification.py infra/worker/runtime/release_readiness.py \
		infra/runtime/manager-secrets-init.py infra/runtime/maintenance-db-authz.py \
		installers/common/image_bundle.py installers/common/publish_release_bundle.py \
		tools/generate_supply_chain_report.py tools/verify_release_source.py

test-web:
	cd apps/web && npm test
	cd apps/web && npm run lint
	cd apps/web && npm run build

syntax-shell:
	@rg --files installers infra tests/infra tests/recovery -g '*.sh' | sort | xargs -n1 bash -n

check: lint-python typecheck-python test-python test-web syntax-shell
	git diff --check
