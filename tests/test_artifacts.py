"""Tests for artifact directory, API endpoints, and path helpers."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# --- create_artifact_dir tests ---


class TestCreateArtifactDir:
    def test_with_ticket_id(self, tmp_path, monkeypatch):
        import paths

        monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)

        from paths import create_artifact_dir

        d = create_artifact_dir("PERF-123", "abc123")
        assert d.exists()
        assert d == tmp_path / "PERF-123" / "abc123"

    def test_without_ticket_id(self, monkeypatch):
        import paths

        monkeypatch.setattr(paths, "ARTIFACT_DIR", Path("/tmp/test-artifacts"))

        from paths import create_artifact_dir

        d = create_artifact_dir("", "abc123")
        assert d.exists()
        assert "abc123" in str(d)
        d.rmdir()

    def test_multiple_runs_same_ticket(self, tmp_path, monkeypatch):
        import paths

        monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)

        from paths import create_artifact_dir

        d1 = create_artifact_dir("PERF-123", "run1")
        d2 = create_artifact_dir("PERF-123", "run2")
        assert d1 != d2
        assert d1.parent == d2.parent
        assert d1.parent.name == "PERF-123"


# --- Artifact API endpoint tests ---


@pytest.fixture
def artifact_app(tmp_path, monkeypatch):
    import paths
    import state_store.api.artifacts as artifacts_mod

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(artifacts_mod, "ARTIFACT_DIR", tmp_path)

    from state_store.main import create_app

    app = create_app()
    return app, tmp_path


@pytest.fixture
def client(artifact_app):
    app, _ = artifact_app
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return c


@pytest.fixture
def artifact_dir(artifact_app):
    _, tmp_path = artifact_app
    return tmp_path


class TestListArtifacts:
    def test_empty_ticket(self, client):
        r = client.get("/api/v1/tickets/PERF-EMPTY/artifacts")
        assert r.status_code == 200
        data = r.json()
        assert data["artifacts"] == []

    def test_with_files(self, client, artifact_dir):
        d = artifact_dir / "PERF-123" / "run-abc"
        d.mkdir(parents=True)
        (d / "results.json").write_text('{"ok": true}')
        (d / "log.txt").write_text("boot log data")

        r = client.get("/api/v1/tickets/PERF-123/artifacts")
        assert r.status_code == 200
        data = r.json()
        assert data["total_files"] == 2
        assert data["total_bytes"] > 0
        paths = [a["path"] for a in data["artifacts"]]
        assert "run-abc/log.txt" in paths
        assert "run-abc/results.json" in paths

    def test_includes_mtime(self, client, artifact_dir):
        d = artifact_dir / "PERF-123"
        d.mkdir(parents=True)
        (d / "file.txt").write_text("data")

        r = client.get("/api/v1/tickets/PERF-123/artifacts")
        data = r.json()
        assert "modified" in data["artifacts"][0]
        assert isinstance(data["artifacts"][0]["modified"], float)

    def test_invalid_ticket_id(self, client):
        r = client.get("/api/v1/tickets/..../artifacts")
        assert r.status_code == 400


class TestDownloadArtifact:
    def test_download_file(self, client, artifact_dir):
        d = artifact_dir / "PERF-123" / "run-abc"
        d.mkdir(parents=True)
        (d / "results.json").write_text('{"data": 42}')

        r = client.get(
            "/api/v1/tickets/PERF-123/artifacts/download/run-abc/results.json"
        )
        assert r.status_code == 200
        assert r.json() == {"data": 42}

    def test_file_not_found(self, client, artifact_dir):
        d = artifact_dir / "PERF-123"
        d.mkdir(parents=True)

        r = client.get("/api/v1/tickets/PERF-123/artifacts/download/nonexistent.txt")
        assert r.status_code == 404

    def test_path_traversal_blocked(self, client, artifact_dir):
        d = artifact_dir / "PERF-123"
        d.mkdir(parents=True)

        r = client.get("/api/v1/tickets/PERF-123/artifacts/download/../../etc/passwd")
        assert r.status_code in (403, 404)

    def test_invalid_ticket_id(self, client):
        r = client.get("/api/v1/tickets/.../artifacts/download/file.txt")
        assert r.status_code == 400


class TestDownloadArchive:
    def test_archive(self, client, artifact_dir):
        d = artifact_dir / "PERF-123" / "run-abc"
        d.mkdir(parents=True)
        (d / "results.json").write_text('{"ok": true}')
        (d / "log.txt").write_text("boot log")

        r = client.get("/api/v1/tickets/PERF-123/artifacts/archive")
        assert r.status_code == 200
        assert "application/gzip" in r.headers["content-type"]
        assert "PERF-123-artifacts.tar.gz" in r.headers.get("content-disposition", "")

        # Verify archive contents
        import io

        buf = io.BytesIO(r.content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()
            assert any("results.json" in n for n in names)
            assert any("log.txt" in n for n in names)

    def test_no_artifacts_404(self, client):
        r = client.get("/api/v1/tickets/PERF-EMPTY/artifacts/archive")
        assert r.status_code == 404

    def test_invalid_ticket_id(self, client):
        r = client.get("/api/v1/tickets/.../artifacts/archive")
        assert r.status_code == 400
