"""Tests for directive normalization in ticket API endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from state_store.main import create_app
from state_store.store import TicketStore


@pytest.fixture
def client(tmp_path):
    app = create_app(initialize_immediately=True)
    app.state.store = TicketStore(persist_dir=tmp_path / "tickets")
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return c


@pytest.fixture
def ticket_id(client):
    r = client.post(
        "/api/v1/tickets",
        json={"summary": "test", "description": "desc"},
    )
    assert r.status_code == 200
    return r.json()["id"]


class TestDirectiveNormalizationOnPatch:
    def test_aliases_normalized_on_patch(self, client, ticket_id):
        """Boot-time aliases are normalized when written via PATCH."""
        # Ensure boot_time aliases are registered
        import providers.skills.boot_time  # noqa: F401

        r = client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={
                "fields": {
                    "directives": {
                        "harness": "boot-time",
                        "delay_seconds": 20,
                        "samples": 5,
                    }
                }
            },
        )
        assert r.status_code == 200

        ticket = client.get(f"/api/v1/tickets/{ticket_id}").json()
        directives = ticket["custom_fields"]["directives"]
        assert "power_off_delay" in directives
        assert directives["power_off_delay"] == 20
        assert "sample_count" in directives
        assert directives["sample_count"] == 5
        assert "delay_seconds" not in directives
        assert "samples" not in directives

    def test_non_directive_fields_unaffected(self, client, ticket_id):
        """Fields other than directives pass through unchanged."""
        r = client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"hypothesis": "test hypothesis"}},
        )
        assert r.status_code == 200
        ticket = client.get(f"/api/v1/tickets/{ticket_id}").json()
        assert ticket["custom_fields"]["hypothesis"] == "test hypothesis"


class TestDirectiveNormalizationOnCreate:
    def test_aliases_normalized_on_create(self, client):
        """Boot-time aliases are normalized at ticket creation."""
        import providers.skills.boot_time  # noqa: F401

        r = client.post(
            "/api/v1/tickets",
            json={
                "summary": "test normalization",
                "description": "desc",
                "custom_fields": {
                    "directives": {
                        "harness": "boot-time",
                        "delay_seconds": 30,
                        "cycles": 10,
                    }
                },
            },
        )
        assert r.status_code == 200
        directives = r.json()["custom_fields"]["directives"]
        assert "power_off_delay" in directives
        assert directives["power_off_delay"] == 30
        assert "sample_count" in directives
        assert directives["sample_count"] == 10


class TestNormalizationComment:
    def test_patch_normalization_adds_comment(self, client, ticket_id):
        """Normalization adds a visible comment to the ticket."""
        import providers.skills.boot_time  # noqa: F401

        client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={
                "fields": {"directives": {"harness": "boot-time", "delay_seconds": 20}}
            },
        )

        ticket = client.get(f"/api/v1/tickets/{ticket_id}").json()
        comments = ticket.get("comments", [])
        norm_comments = [
            c for c in comments if "normalization" in c.get("body", "").lower()
        ]
        assert len(norm_comments) == 1
        assert "delay_seconds" in norm_comments[0]["body"]
        assert "power_off_delay" in norm_comments[0]["body"]
        assert norm_comments[0]["author"] == "system"

    def test_create_normalization_adds_comment(self, client):
        """Normalization at creation adds a comment."""
        import providers.skills.boot_time  # noqa: F401

        r = client.post(
            "/api/v1/tickets",
            json={
                "summary": "test",
                "description": "desc",
                "custom_fields": {"directives": {"harness": "boot-time", "cycles": 10}},
            },
        )
        tid = r.json()["id"]
        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        comments = ticket.get("comments", [])
        norm_comments = [
            c for c in comments if "normalization" in c.get("body", "").lower()
        ]
        assert len(norm_comments) == 1
        assert "cycles" in norm_comments[0]["body"]
        assert "sample_count" in norm_comments[0]["body"]

    def test_no_comment_when_no_aliases(self, client, ticket_id):
        """No comment when directives use canonical keys."""
        client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={
                "fields": {
                    "directives": {"harness": "boot-time", "power_off_delay": 20}
                }
            },
        )

        ticket = client.get(f"/api/v1/tickets/{ticket_id}").json()
        comments = ticket.get("comments", [])
        norm_comments = [
            c for c in comments if "normalization" in c.get("body", "").lower()
        ]
        assert len(norm_comments) == 0
