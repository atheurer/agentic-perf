#!/usr/bin/env python3
"""Validate the installed dependency set before running the test suite."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping

from packaging.version import InvalidVersion, Version

REQUIRED_PACKAGES = (
    "httpx2",
    "opentelemetry-api",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-instrumentation",
    "opentelemetry-instrumentation-anthropic",
    "opentelemetry-instrumentation-openai",
    "opentelemetry-sdk",
    "opentelemetry-semantic-conventions",
    "opentelemetry-semantic-conventions-ai",
    "starlette",
)


def validate_versions(installed: Mapping[str, str]) -> list[str]:
    """Return human-readable errors for an incompatible dependency set."""
    errors: list[str] = []

    for package in REQUIRED_PACKAGES:
        if not installed.get(package):
            errors.append(f"{package} is not installed")

    if errors:
        return errors

    if installed["opentelemetry-api"] != installed["opentelemetry-sdk"]:
        errors.append(
            "OpenTelemetry API and SDK versions must match: "
            f"api={installed['opentelemetry-api']}, "
            f"sdk={installed['opentelemetry-sdk']}"
        )

    exporter = "opentelemetry-exporter-otlp-proto-grpc"
    if installed["opentelemetry-api"] != installed[exporter]:
        errors.append(
            "OpenTelemetry API and OTLP exporter versions must match: "
            f"api={installed['opentelemetry-api']}, "
            f"exporter={installed[exporter]}"
        )

    anthropic = "opentelemetry-instrumentation-anthropic"
    openai = "opentelemetry-instrumentation-openai"
    if installed[anthropic] != installed[openai]:
        errors.append(
            "OpenTelemetry LLM instrumentation versions must match: "
            f"anthropic={installed[anthropic]}, "
            f"openai={installed[openai]}"
        )

    if (
        installed["opentelemetry-instrumentation"]
        != installed["opentelemetry-semantic-conventions"]
    ):
        errors.append(
            "OpenTelemetry instrumentation and semantic conventions versions "
            "must match: "
            f"instrumentation={installed['opentelemetry-instrumentation']}, "
            "semantic-conventions="
            f"{installed['opentelemetry-semantic-conventions']}"
        )

    try:
        instrumentation_version = Version(installed["opentelemetry-instrumentation"])
        if instrumentation_version < Version("0.59b0"):
            errors.append(
                "OpenTelemetry instrumentation is too old for LLM "
                f"instrumentation 0.62.x: {instrumentation_version}"
            )
    except InvalidVersion as error:
        errors.append(f"Invalid OpenTelemetry instrumentation version: {error}")

    try:
        semantic_conventions_version = Version(
            installed["opentelemetry-semantic-conventions"]
        )
        if semantic_conventions_version < Version("0.63b1"):
            errors.append(
                "OpenTelemetry semantic conventions are too old: "
                f"{semantic_conventions_version}"
            )
    except InvalidVersion as error:
        errors.append(f"Invalid OpenTelemetry semantic conventions version: {error}")

    try:
        semantic_conventions_ai_version = Version(
            installed["opentelemetry-semantic-conventions-ai"]
        )
        if not (Version("0.5.1") <= semantic_conventions_ai_version < Version("0.6")):
            errors.append(
                "OpenTelemetry AI semantic conventions must be in the 0.5.x "
                f"series: {semantic_conventions_ai_version}"
            )
    except InvalidVersion as error:
        errors.append(f"Invalid OpenTelemetry AI semantic conventions version: {error}")

    return errors


def main() -> int:
    installed: dict[str, str] = {}
    for package in REQUIRED_PACKAGES:
        try:
            installed[package] = version(package)
        except PackageNotFoundError:
            continue

    errors = validate_versions(installed)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Dependency preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
