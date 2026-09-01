from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents.server_utils import read_skill_document, read_skill_documents
from providers.skills.repo_cache import RepoCache


def _benchmark_server():
    return importlib.import_module("agents.benchmark.server")


def _provisioning_server():
    return importlib.import_module("agents.provisioning.server")


def _review_server():
    return importlib.import_module("agents.review.server")


@pytest.fixture
def mock_skills_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    general = skills / "general"
    general.mkdir(parents=True)
    (general / "host-tuning.md").write_text("# Host Tuning Guide\nContent here.")
    (general / "network-manager.md").write_text("# Network Manager\nContent here.")

    crucible = skills / "crucible"
    crucible.mkdir(parents=True)
    (crucible / "run-file-pitfalls.md").write_text("# Runfile Pitfalls\nContent here.")
    return skills


@pytest.fixture
def mock_repo_cache(tmp_path: Path) -> RepoCache:
    cache_dir = tmp_path / "repos"
    crucible_repo = cache_dir / "crucible"
    docs_dir = crucible_repo / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "how-run-files-work.md").write_text(
        "# How Run Files Work\nDocs content."
    )

    config_dir = crucible_repo / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "default.yml").write_text("setting: true\n")

    cache = RepoCache(cache_dir=cache_dir)
    return cache


class TestSkillDocumentNormalization:
    def test_exact_path(self, mock_skills_dir: Path):
        res = read_skill_document(mock_skills_dir, "general", "host-tuning.md")
        assert res["found"] is True
        assert res["filename"] == "host-tuning.md"
        assert res["harness"] == "general"
        assert "Host Tuning Guide" in res["content"]

    def test_redundant_harness_prefix(self, mock_skills_dir: Path):
        res = read_skill_document(mock_skills_dir, "general", "general/host-tuning.md")
        assert res["found"] is True
        assert res["filename"] == "host-tuning.md"
        assert "Host Tuning Guide" in res["content"]

    def test_redundant_skills_prefix(self, mock_skills_dir: Path):
        res = read_skill_document(
            mock_skills_dir, "general", "skills/general/host-tuning.md"
        )
        assert res["found"] is True
        assert res["filename"] == "host-tuning.md"
        assert "Host Tuning Guide" in res["content"]

    def test_skills_prefix_without_harness_in_filename(self, mock_skills_dir: Path):
        res = read_skill_document(mock_skills_dir, "general", "skills/host-tuning.md")
        assert res["found"] is True
        assert res["filename"] == "host-tuning.md"
        assert "Host Tuning Guide" in res["content"]

    def test_empty_harness_with_path_in_filename(self, mock_skills_dir: Path):
        res = read_skill_document(mock_skills_dir, "", "general/host-tuning.md")
        assert res["found"] is True
        assert res["harness"] == "general"
        assert res["filename"] == "host-tuning.md"
        assert "Host Tuning Guide" in res["content"]

    def test_subdirectory_fallback(self, mock_skills_dir: Path):
        res = read_skill_document(mock_skills_dir, "general", "docs/host-tuning.md")
        assert res["found"] is True
        assert res["filename"] == "host-tuning.md"
        assert "Host Tuning Guide" in res["content"]

    def test_mismatched_harness_fallback(self, mock_skills_dir: Path):
        res = read_skill_document(mock_skills_dir, "crucible", "general/host-tuning.md")
        assert res["found"] is True
        assert res["harness"] == "general"
        assert res["filename"] == "host-tuning.md"
        assert "Host Tuning Guide" in res["content"]

    def test_not_found(self, mock_skills_dir: Path):
        res = read_skill_document(mock_skills_dir, "general", "nonexistent.md")
        assert res["found"] is False
        assert "Skill not found" in res["message"]

    def test_path_traversal_blocked(self, mock_skills_dir: Path):
        res = read_skill_document(mock_skills_dir, "general", "../../../etc/passwd")
        assert res["found"] is False


class TestProvisioningSkillTools:
    @pytest.mark.asyncio
    async def test_read_skills_single_normalization(self, mock_skills_dir: Path):
        with patch("agents.provisioning.server._SKILLS_DIR", mock_skills_dir):
            res_str = await _provisioning_server().read_skills(
                [{"harness": "general", "filename": "general/host-tuning.md"}]
            )
            res = json.loads(res_str)
            assert len(res) == 1
            assert res[0]["found"] is True
            assert "Host Tuning Guide" in res[0]["content"]

    @pytest.mark.asyncio
    async def test_read_skills_batch(self, mock_skills_dir: Path):
        with patch("agents.provisioning.server._SKILLS_DIR", mock_skills_dir):
            docs = [
                {"harness": "general", "filename": "general/host-tuning.md"},
                {"harness": "", "filename": "general/network-manager.md"},
                {"name": "skills/crucible/run-file-pitfalls.md"},
            ]
            res_str = await _provisioning_server().read_skills(docs)
            res = json.loads(res_str)
            assert len(res) == 3
            assert res[0]["found"] is True
            assert res[0]["filename"] == "host-tuning.md"
            assert res[1]["found"] is True
            assert res[1]["filename"] == "network-manager.md"
            assert res[2]["found"] is True
            assert res[2]["filename"] == "run-file-pitfalls.md"


class TestBenchmarkSkillAndDocTools:
    @pytest.mark.asyncio
    async def test_read_skills_single_normalization(self, mock_skills_dir: Path):
        with (
            patch("agents.benchmark.server.SKILLS_DIR", mock_skills_dir),
            patch("agents.benchmark.server._ensure_init", new_callable=AsyncMock),
        ):
            res_str = await _benchmark_server().read_skills(
                [
                    {
                        "harness": "crucible",
                        "filename": "skills/crucible/run-file-pitfalls.md",
                    }
                ]
            )
            res = json.loads(res_str)
            assert len(res) == 1
            assert res[0]["found"] is True
            assert res[0]["filename"] == "run-file-pitfalls.md"

    @pytest.mark.asyncio
    async def test_read_skills_batch(self, mock_skills_dir: Path):
        with (
            patch("agents.benchmark.server.SKILLS_DIR", mock_skills_dir),
            patch("agents.benchmark.server._ensure_init", new_callable=AsyncMock),
        ):
            docs = [
                {"harness": "crucible", "filename": "crucible/run-file-pitfalls.md"},
                {"harness": "general", "filename": "general/host-tuning.md"},
            ]
            res_str = await _benchmark_server().read_skills(docs)
            res = json.loads(res_str)
            assert len(res) == 2
            assert res[0]["found"] is True
            assert res[1]["found"] is True

    @pytest.mark.asyncio
    async def test_read_harness_doc_normalization(self, mock_repo_cache: RepoCache):
        with (
            patch("agents.benchmark.server._repo_cache", mock_repo_cache),
            patch("agents.benchmark.server._ensure_init", new_callable=AsyncMock),
        ):
            # Exact path with docs/
            res1 = json.loads(
                await _benchmark_server().read_harness_doc(
                    "crucible", "docs/how-run-files-work.md"
                )
            )
            assert res1["found"] is True
            assert "How Run Files Work" in res1["content"]

            # Missing docs/
            res2 = json.loads(
                await _benchmark_server().read_harness_doc(
                    "crucible", "how-run-files-work.md"
                )
            )
            assert res2["found"] is True
            assert "How Run Files Work" in res2["content"]

            # Redundant harness prefix
            res3 = json.loads(
                await _benchmark_server().read_harness_doc(
                    "crucible", "crucible/docs/how-run-files-work.md"
                )
            )
            assert res3["found"] is True

            # Empty harness with full path
            res4 = json.loads(
                await _benchmark_server().read_harness_doc(
                    "", "crucible/docs/how-run-files-work.md"
                )
            )
            assert res4["found"] is True


class TestRepoCacheReadFile:
    def test_exact_path(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file("crucible", "docs/how-run-files-work.md")
        assert content is not None
        assert "How Run Files Work" in content

    def test_missing_docs_prefix(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file("crucible", "how-run-files-work.md")
        assert content is not None
        assert "How Run Files Work" in content

    def test_redundant_harness_prefix(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file(
            "crucible", "crucible/docs/how-run-files-work.md"
        )
        assert content is not None
        assert "How Run Files Work" in content

    def test_redundant_harness_prefix_no_docs(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file(
            "crucible", "crucible/how-run-files-work.md"
        )
        assert content is not None
        assert "How Run Files Work" in content

    def test_redundant_docs_prefix(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file(
            "crucible", "docs/docs/how-run-files-work.md"
        )
        assert content is not None
        assert "How Run Files Work" in content

    def test_config_dir_file(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file("crucible", "config/default.yml")
        assert content is not None
        assert "setting: true" in content

        content_without_config = mock_repo_cache.read_file("crucible", "default.yml")
        assert content_without_config is not None
        assert "setting: true" in content_without_config

    def test_file_not_found(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file("crucible", "nonexistent.md")
        assert content is None

    def test_repo_not_found(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file("unknown_harness", "docs/foo.md")
        assert content is None

    def test_path_traversal_blocked(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file("crucible", "../../../etc/passwd")
        assert content is None

    def test_empty_repo_name(self, mock_repo_cache: RepoCache):
        content = mock_repo_cache.read_file("", "docs/foo.md")
        assert content is None


class TestReviewDocTools:
    @pytest.mark.asyncio
    async def test_read_skills_single_normalization(self, mock_skills_dir: Path):
        with (
            patch("agents.review.server.SKILLS_DIR", mock_skills_dir),
            patch("agents.review.server._ensure_init", new_callable=AsyncMock),
        ):
            res_str = await _review_server().read_skills(
                [{"harness": "general", "filename": "general/host-tuning.md"}]
            )
            res = json.loads(res_str)
            assert len(res) == 1
            assert res[0]["found"] is True
            assert "Host Tuning Guide" in res[0]["content"]

    @pytest.mark.asyncio
    async def test_read_harness_doc_normalization(self, mock_repo_cache: RepoCache):
        with (
            patch("agents.review.server._repo_cache", mock_repo_cache),
            patch("agents.review.server._ensure_init", new_callable=AsyncMock),
        ):
            res_str = await _review_server().read_harness_doc(
                "crucible", "how-run-files-work.md"
            )
            res = json.loads(res_str)
            assert res["status"] == "ok"
            assert "How Run Files Work" in res["content"]


class TestAnalyzeAndTriageSkillTools:
    @pytest.mark.asyncio
    async def test_analyze_read_skills_normalization(self, mock_skills_dir: Path):
        from agents.analyze.server import read_skills as analyze_read_skills

        with patch("agents.analyze.server.SKILLS_DIR", mock_skills_dir):
            res_str = await analyze_read_skills(
                [{"harness": "general", "filename": "skills/general/host-tuning.md"}]
            )
            res = json.loads(res_str)
            assert len(res) == 1
            assert res[0]["found"] is True
            assert res[0]["filename"] == "host-tuning.md"
            assert "Host Tuning Guide" in res[0]["content"]

    @pytest.mark.asyncio
    async def test_triage_read_skills_normalization(self, mock_skills_dir: Path):
        from agents.triage.server import read_skills as triage_read_skills

        with patch("agents.triage.server.SKILLS_DIR", mock_skills_dir):
            res_str = await triage_read_skills(
                [{"harness": "general", "filename": "general/host-tuning.md"}]
            )
            res = json.loads(res_str)
            assert len(res) == 1
            assert res[0]["found"] is True
            assert res[0]["filename"] == "host-tuning.md"
            assert "Host Tuning Guide" in res[0]["content"]


class TestReadSkillDocumentsBatch:
    def test_batch_returns_all_results(self, mock_skills_dir: Path):
        docs = [
            {"harness": "general", "filename": "host-tuning.md"},
            {"harness": "crucible", "filename": "run-file-pitfalls.md"},
        ]
        results = read_skill_documents(mock_skills_dir, docs)
        assert len(results) == 2
        assert results[0]["found"] is True
        assert results[0]["filename"] == "host-tuning.md"
        assert results[1]["found"] is True
        assert results[1]["filename"] == "run-file-pitfalls.md"

    def test_batch_category_alias(self, mock_skills_dir: Path):
        docs = [{"category": "general", "filename": "host-tuning.md"}]
        results = read_skill_documents(mock_skills_dir, docs)
        assert len(results) == 1
        assert results[0]["found"] is True

    def test_batch_name_alias(self, mock_skills_dir: Path):
        docs = [{"harness": "general", "name": "host-tuning.md"}]
        results = read_skill_documents(mock_skills_dir, docs)
        assert len(results) == 1
        assert results[0]["found"] is True

    def test_batch_harness_wins_over_category(self, mock_skills_dir: Path):
        docs = [
            {
                "harness": "general",
                "category": "crucible",
                "filename": "host-tuning.md",
            }
        ]
        results = read_skill_documents(mock_skills_dir, docs)
        assert results[0]["found"] is True
        assert results[0]["harness"] == "general"

    def test_batch_filename_wins_over_name(self, mock_skills_dir: Path):
        docs = [
            {
                "harness": "general",
                "filename": "host-tuning.md",
                "name": "network-manager.md",
            }
        ]
        results = read_skill_documents(mock_skills_dir, docs)
        assert results[0]["found"] is True
        assert results[0]["filename"] == "host-tuning.md"

    def test_batch_empty_harness_falls_through_to_category(self, mock_skills_dir: Path):
        docs = [{"harness": "", "category": "general", "filename": "host-tuning.md"}]
        results = read_skill_documents(mock_skills_dir, docs)
        assert results[0]["found"] is True

    def test_batch_not_found_entry(self, mock_skills_dir: Path):
        docs = [
            {"harness": "general", "filename": "host-tuning.md"},
            {"harness": "general", "filename": "nonexistent.md"},
        ]
        results = read_skill_documents(mock_skills_dir, docs)
        assert results[0]["found"] is True
        assert results[1]["found"] is False

    def test_batch_empty_list(self, mock_skills_dir: Path):
        results = read_skill_documents(mock_skills_dir, [])
        assert results == []

    def test_batch_preserves_order(self, mock_skills_dir: Path):
        docs = [
            {"harness": "crucible", "filename": "run-file-pitfalls.md"},
            {"harness": "general", "filename": "network-manager.md"},
            {"harness": "general", "filename": "host-tuning.md"},
        ]
        results = read_skill_documents(mock_skills_dir, docs)
        assert [r["filename"] for r in results] == [
            "run-file-pitfalls.md",
            "network-manager.md",
            "host-tuning.md",
        ]
