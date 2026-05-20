from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_manager_web_allows_only_the_configured_presign_origin() -> None:
    compose = yaml.safe_load(
        (ROOT / "infra/compose/manager.compose.yml").read_text(encoding="utf-8")
    )
    web = compose["services"]["web"]

    assert web["environment"]["DATASET_UPLOAD_ALLOWED_ORIGINS"] == (
        "${S3_PRESIGN_ENDPOINT_URL:?S3_PRESIGN_ENDPOINT_URL must be reachable by browsers}"
    )
    assert "JWT_SECRET" not in web["environment"]


def test_minio_browser_upload_cors_is_explicit_and_noncredentialed() -> None:
    compose = yaml.safe_load(
        (ROOT / "infra/compose/manager.compose.yml").read_text(encoding="utf-8")
    )
    environment = compose["services"]["minio"]["environment"]

    assert environment["MINIO_API_CORS_ALLOW_ORIGIN"] == (
        "${CORS_ORIGINS:-http://localhost:8080}"
    )
    assert environment["MINIO_API_CORS_ALLOW_CREDENTIALS_WITH_WILDCARD"] == "off"
