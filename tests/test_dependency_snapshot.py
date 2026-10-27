"""Tests for the dependency inventory sent to GitHub."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "scripts" / "dependency_snapshot.py"
spec = importlib.util.spec_from_file_location("dependency_snapshot", PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_extracts_hashed_pins_and_normalizes_names() -> None:
    resolved = module.parse_lock(
        "# pip-compile\nExample_Pkg[extra]==1.2.3 \\\n"
        "    --hash=sha256:123 \\\n"
        "    --hash=sha256:456\n",
        "requirements.lock",
    )
    assert resolved == {
        "pkg:pypi/example-pkg@1.2.3": {
            "package_url": "pkg:pypi/example-pkg@1.2.3",
            "relationship": "indirect",
            "scope": "runtime",
        }
    }


@pytest.mark.parametrize(
    "content",
    [
        "example>=1.0\n",
        "example==1.0; python_version > '3.12'\n",
        "# empty\n",
        "foo==1\nFoo==2\n",
    ],
)
def test_rejects_unrepresentable_or_ambiguous_locks(content: str) -> None:
    with pytest.raises(ValueError):
        module.parse_lock(content, "requirements.lock")


def test_real_locks_produce_versioned_inventory() -> None:
    root = PATH.parents[1]
    result = module.snapshot(root, "abc123", "42")
    assert result["sha"] == "abc123"
    manifests = result["manifests"]
    assert set(manifests) == {"requirements.lock", "requirements-dev.lock"}
    for data in manifests.values():
        assert data["resolved"]
        assert all("@" in purl for purl in data["resolved"])
