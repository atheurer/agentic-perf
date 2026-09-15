"""Tests for state store mutation audit trail.

Covers: audit entries for every mutation type, actor propagation,
sequence monotonicity, backward compatibility, reload recovery,
and the GET /api/v1/audit query endpoint.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from state_store.audit import AuditLog, set_actor
from state_store.main import create_app
from state_store.models import (
    AddCommentRequest,
    CreateTicketRequest,
    TicketStatus,
    TransitionRequest,
)
from state_store.store import TicketStore


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def audit_log(audit_path):
    log = AuditLog(path=audit_path)
    yield log
    log.close()


@pytest.fixture
def store(tmp_path, audit_log):
    return TicketStore(persist_dir=tmp_path / "tickets", audit_log=audit_log)


@pytest.fixture
def ticket(store):
    return store.create_ticket(
        CreateTicketRequest(summary="test ticket", description="desc"),
    )


def _read_entries(audit_path):
    if not audit_path.exists():
        return []
    entries = []
    for line in audit_path.read_text().strip().splitlines():
        entries.append(json.loads(line))
    return entries


class TestTransitionAudit:
    def test_transition_creates_audit_entry(self, store, ticket, audit_path):
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending"),
        )
        entries = _read_entries(audit_path)
        transitions = [e for e in entries if e["mutation"] == "transition_ticket"]
        assert len(transitions) == 1
        t = transitions[0]
        assert t["ticket_id"] == ticket.id
        assert t["data"]["old_status"] == "new"
        assert t["data"]["new_status"] == "triage_pending"

    def test_transition_with_comment(self, store, ticket, audit_path):
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending", comment="starting triage"),
        )
        entries = _read_entries(audit_path)
        transitions = [e for e in entries if e["mutation"] == "transition_ticket"]
        assert transitions[0]["data"]["comment"] == "starting triage"


class TestCreateAudit:
    def test_create_ticket_audited(self, store, audit_path):
        t = store.create_ticket(
            CreateTicketRequest(summary="my summary", description="desc"),
        )
        entries = _read_entries(audit_path)
        creates = [e for e in entries if e["mutation"] == "create_ticket"]
        assert len(creates) >= 1
        last = creates[-1]
        assert last["ticket_id"] == t.id
        assert last["data"]["summary"] == "my summary"

    def test_summary_truncated(self, store, audit_path):
        long_summary = "x" * 300
        store.create_ticket(
            CreateTicketRequest(summary=long_summary, description="desc"),
        )
        entries = _read_entries(audit_path)
        creates = [e for e in entries if e["mutation"] == "create_ticket"]
        assert len(creates[-1]["data"]["summary"]) == 200


class TestUpdateFieldsAudit:
    def test_logs_keys_not_values(self, store, ticket, audit_path):
        store.update_fields(ticket.id, {"foo": "bar", "baz": {"nested": "big"}})
        entries = _read_entries(audit_path)
        updates = [e for e in entries if e["mutation"] == "update_fields"]
        assert len(updates) == 1
        assert updates[0]["data"]["field_names"] == ["baz", "foo"]
        assert "bar" not in json.dumps(updates[0]["data"])


class TestForceCloseAudit:
    def test_force_close_audited(self, store, ticket, audit_path):
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending"),
        )
        store.force_close(ticket.id, comment="shutting down")
        entries = _read_entries(audit_path)
        closes = [e for e in entries if e["mutation"] == "force_close"]
        assert len(closes) == 1
        assert closes[0]["data"]["old_status"] == "triage_pending"
        assert closes[0]["data"]["comment"] == "shutting down"

    def test_force_close_already_closed(self, store, ticket, audit_path):
        store.force_close(ticket.id)
        store.force_close(ticket.id)
        entries = _read_entries(audit_path)
        closes = [e for e in entries if e["mutation"] == "force_close"]
        assert len(closes) == 2
        assert closes[1]["data"]["result"] == "already_closed"


class TestClaimAudit:
    def test_claim_success_audited(self, store, ticket, audit_path):
        store.claim_ticket(ticket.id, "orch-1", 300)
        entries = _read_entries(audit_path)
        claims = [e for e in entries if e["mutation"] == "claim_ticket"]
        assert len(claims) == 1
        assert claims[0]["data"]["result"] == "claimed"
        assert claims[0]["data"]["owner"] == "orch-1"

    def test_claim_reject_audited(self, store, ticket, audit_path):
        store.claim_ticket(ticket.id, "orch-1", 300)
        store.claim_ticket(ticket.id, "orch-2", 300)
        entries = _read_entries(audit_path)
        claims = [e for e in entries if e["mutation"] == "claim_ticket"]
        rejected = [c for c in claims if c["data"]["result"] == "rejected"]
        assert len(rejected) == 1
        assert rejected[0]["data"]["held_by"] == "orch-1"

    def test_release_claim_audited(self, store, ticket, audit_path):
        store.claim_ticket(ticket.id, "orch-1", 300)
        store.release_claim(ticket.id, "orch-1")
        entries = _read_entries(audit_path)
        releases = [e for e in entries if e["mutation"] == "release_claim"]
        assert len(releases) == 1
        assert releases[0]["data"]["result"] == "released"

    def test_release_not_owner_audited(self, store, ticket, audit_path):
        store.claim_ticket(ticket.id, "orch-1", 300)
        store.release_claim(ticket.id, "orch-2")
        entries = _read_entries(audit_path)
        releases = [e for e in entries if e["mutation"] == "release_claim"]
        assert len(releases) == 1
        assert releases[0]["data"]["result"] == "not_owner"

    def test_renew_claim_audited(self, store, ticket, audit_path):
        store.claim_ticket(ticket.id, "orch-1", 300)
        store.renew_claim(ticket.id, "orch-1", 600)
        entries = _read_entries(audit_path)
        renews = [e for e in entries if e["mutation"] == "renew_claim"]
        assert len(renews) == 1
        assert renews[0]["data"]["result"] == "renewed"

    def test_renew_not_owner_audited(self, store, ticket, audit_path):
        store.claim_ticket(ticket.id, "orch-1", 300)
        store.renew_claim(ticket.id, "orch-2", 600)
        entries = _read_entries(audit_path)
        renews = [e for e in entries if e["mutation"] == "renew_claim"]
        assert len(renews) == 1
        assert renews[0]["data"]["result"] == "not_owner"


class TestSetOwnersAudit:
    def test_set_owners_audited(self, store, ticket, audit_path):
        store.set_owners(ticket.id, ["alice", "bob"])
        entries = _read_entries(audit_path)
        owner_changes = [e for e in entries if e["mutation"] == "set_owners"]
        assert len(owner_changes) == 1
        assert owner_changes[0]["data"]["old_owners"] == []
        assert owner_changes[0]["data"]["new_owners"] == ["alice", "bob"]


class TestCommentAudit:
    def test_add_comment_audited(self, store, ticket, audit_path):
        store.add_comment(
            ticket.id,
            AddCommentRequest(author="tester", body="hello"),
        )
        entries = _read_entries(audit_path)
        comments = [e for e in entries if e["mutation"] == "add_comment"]
        assert len(comments) == 1
        assert comments[0]["data"]["author"] == "tester"
        assert "comment_id" in comments[0]["data"]


class TestActorPropagation:
    def test_actor_in_entry(self, store, ticket, audit_path):
        set_actor("service", "deployment", "127.0.0.1")
        store.update_fields(ticket.id, {"key": "val"})
        entries = _read_entries(audit_path)
        updates = [e for e in entries if e["mutation"] == "update_fields"]
        actor = updates[0]["actor"]
        assert actor["kind"] == "service"
        assert actor["username"] == "deployment"
        assert actor["ip"] == "127.0.0.1"


class TestSequenceOrdering:
    def test_seq_monotonic(self, store, ticket, audit_path):
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending"),
        )
        store.update_fields(ticket.id, {"x": 1})
        store.add_comment(
            ticket.id,
            AddCommentRequest(author="a", body="b"),
        )
        entries = _read_entries(audit_path)
        seqs = [e["seq"] for e in entries]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)


class TestBackwardCompat:
    def test_store_works_without_audit(self, tmp_path):
        store = TicketStore(persist_dir=tmp_path)
        t = store.create_ticket(
            CreateTicketRequest(summary="no audit", description="test"),
        )
        store.transition_ticket(
            t.id,
            TransitionRequest(status="triage_pending"),
        )
        result = store.get_ticket(t.id)
        assert result.status == TicketStatus.TRIAGE_PENDING


class TestAuditReload:
    def test_seq_continues_after_reload(self, audit_path):
        log1 = AuditLog(path=audit_path)
        log1.log("create_ticket", "PERF-1", {"summary": "first"})
        log1.log("create_ticket", "PERF-2", {"summary": "second"})
        seq_before = log1.latest_seq
        log1.close()

        log2 = AuditLog(path=audit_path)
        log2.log("create_ticket", "PERF-3", {"summary": "third"})
        entries = _read_entries(audit_path)
        assert entries[-1]["seq"] == seq_before + 1
        log2.close()


class TestAuditEndpoint:
    @pytest.fixture
    def app_with_audit(self, tmp_path):
        application = create_app()
        audit = AuditLog(path=tmp_path / "audit.jsonl")
        store = TicketStore(
            persist_dir=tmp_path / "tickets",
            audit_log=audit,
        )
        application.state.store = store
        application.state.audit_log = audit
        return application

    @pytest.fixture
    def client(self, app_with_audit):
        c = TestClient(app_with_audit)
        c.headers["Authorization"] = f"Bearer {app_with_audit.state.api_token}"
        return c

    def test_audit_endpoint_returns_entries(self, client):
        client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "desc"},
        )
        r = client.get("/api/v1/audit")
        assert r.status_code == 200
        data = r.json()
        assert len(data["entries"]) >= 1
        assert data["latest_seq"] >= 1

    def test_audit_filter_by_ticket(self, client):
        r1 = client.post(
            "/api/v1/tickets",
            json={"summary": "ticket A", "description": "desc"},
        )
        tid = r1.json()["id"]
        client.post(
            "/api/v1/tickets",
            json={"summary": "ticket B", "description": "desc"},
        )
        r = client.get(f"/api/v1/audit?ticket_id={tid}")
        data = r.json()
        assert all(e["ticket_id"] == tid for e in data["entries"])

    def test_audit_since_filter(self, client):
        client.post(
            "/api/v1/tickets",
            json={"summary": "first", "description": "desc"},
        )
        r1 = client.get("/api/v1/audit")
        latest = r1.json()["latest_seq"]

        client.post(
            "/api/v1/tickets",
            json={"summary": "second", "description": "desc"},
        )
        r2 = client.get(f"/api/v1/audit?since={latest}")
        entries = r2.json()["entries"]
        assert len(entries) >= 1
        assert all(e["seq"] > latest for e in entries)
