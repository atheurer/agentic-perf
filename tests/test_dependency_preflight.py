"""Tests for the development dependency preflight."""

from scripts.dependency_preflight import validate_versions

COMPATIBLE_VERSIONS = {
    "httpx2": "2.13.1",
    "opentelemetry-api": "1.45.2",
    "opentelemetry-exporter-otlp-proto-grpc": "1.45.2",
    "opentelemetry-instrumentation": "0.66b2",
    "opentelemetry-instrumentation-anthropic": "0.62.4",
    "opentelemetry-instrumentation-openai": "0.62.4",
    "opentelemetry-sdk": "1.45.2",
    "opentelemetry-semantic-conventions": "0.66b2",
    "opentelemetry-semantic-conventions-ai": "0.5.1",
    "starlette": "1.3.1",
}


def test_compatible_versions_pass() -> None:
    assert validate_versions(COMPATIBLE_VERSIONS) == []


def test_api_sdk_drift_is_reported() -> None:
    drifted = dict(COMPATIBLE_VERSIONS)
    drifted["opentelemetry-sdk"] = "1.41.1"

    errors = validate_versions(drifted)

    assert errors == [
        "OpenTelemetry API and SDK versions must match: api=1.45.2, sdk=1.41.1"
    ]


def test_instrumentation_semantic_conventions_drift_is_reported() -> None:
    drifted = dict(COMPATIBLE_VERSIONS)
    drifted["opentelemetry-semantic-conventions"] = "0.66b1"

    errors = validate_versions(drifted)

    assert errors == [
        "OpenTelemetry instrumentation and semantic conventions versions "
        "must match: instrumentation=0.66b2, semantic-conventions=0.66b1"
    ]


def test_missing_packages_are_reported() -> None:
    errors = validate_versions({})

    assert errors == [f"{package} is not installed" for package in COMPATIBLE_VERSIONS]
