"""Tests for the directive normalizer callback in the PATCH /fields endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from state_store.api.tickets import set_directive_normalizer
from state_store.main import create_app
from state_store.store import TicketStore


@pytest.fixture(autouse=True)
def _reset_normalizer():
    """Ensure the global normalizer is cleaned up after each test."""
    yield
    set_directive_normalizer(None)


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


class TestDirectiveNormalizerHook:
    def test_normalizer_applied_on_patch(self, client, ticket_id):
        """When a normalizer is registered, directives are normalized before storage."""
        set_directive_normalizer(lambda d: {k.lower(): v for k, v in d.items()})

        r = client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"directives": {"SKIP_REBOOT": True, "Verbose": False}}},
        )
        assert r.status_code == 200

        ticket = client.get(f"/api/v1/tickets/{ticket_id}").json()
        directives = ticket["custom_fields"]["directives"]
        assert "skip_reboot" in directives
        assert "verbose" in directives
        assert "SKIP_REBOOT" not in directives

    def test_no_normalizer_passes_through(self, client, ticket_id):
        """Without a normalizer registered, directives are stored as-is."""
        r = client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"directives": {"SKIP_REBOOT": True}}},
        )
        assert r.status_code == 200

        ticket = client.get(f"/api/v1/tickets/{ticket_id}").json()
        directives = ticket["custom_fields"]["directives"]
        assert "SKIP_REBOOT" in directives

    def test_non_directive_fields_unaffected(self, client, ticket_id):
        """The normalizer only touches 'directives', not other fields."""
        called = []
        set_directive_normalizer(lambda d: (called.append(1), d)[1])

        r = client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"status_note": "hello"}},
        )
        assert r.status_code == 200
        assert len(called) == 0
