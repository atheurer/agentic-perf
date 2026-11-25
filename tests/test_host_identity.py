"""Host-identity extraction and transcription recovery (#1381)."""

from __future__ import annotations

from providers.host_identity import (
    extract_host_identities,
    resolve_submitted_host,
)

_DESCRIPTION = """
Client: dhcp-10-26-9-207.perf.eng.bos2.dc.redhat.com
Server: u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com
        u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com
Also available: 10.0.0.5 and fd00::1 if needed.
"""


class TestExtractHostIdentities:
    def test_extracts_fqdns_and_ips(self):
        found = extract_host_identities(_DESCRIPTION)
        assert "dhcp-10-26-9-207.perf.eng.bos2.dc.redhat.com" in found
        assert "u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com" in found
        assert "u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com" in found
        assert "10.0.0.5" in found
        assert "fd00::1" in found

    def test_ignores_sentences_and_versions(self):
        found = extract_host_identities(
            "RHEL 9.8 with kernel-rt 5.14.0-70.13.1. Run for 30 seconds."
        )
        assert found == set()

    def test_empty_and_non_string(self):
        assert extract_host_identities("") == set()


class TestResolveSubmittedHost:
    def test_exact_case_insensitive_match(self):
        canonical, reason = resolve_submitted_host(
            "U37-H12-000-R7725.rdu3.labs.perfscale.redhat.com",
            ["u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com"],
        )
        assert canonical == "u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com"
        assert reason == "exact"

    def test_recovers_one_omitted_interior_label(self):
        # The observed failure (#1381): the 'rdu3.' label was dropped.
        canonical, reason = resolve_submitted_host(
            "u37-h14-000-r7725.labs.perfscale.redhat.com",
            [
                "u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com",
                "u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com",
            ],
        )
        assert canonical == "u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com"
        assert "one omitted" in reason

    def test_multiple_label_deletion_is_not_recovered(self):
        canonical, _ = resolve_submitted_host(
            "u37-h14-000-r7725.redhat.com",
            ["u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com"],
        )
        assert canonical is None

    def test_first_or_last_label_change_is_not_recovered(self):
        canonical, _ = resolve_submitted_host(
            "x37-h14-000-r7725.rdu3.labs.perfscale.redhat.com",
            ["u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com"],
        )
        assert canonical is None
        canonical, _ = resolve_submitted_host(
            "u37-h14-000-r7725.rdu3.labs.perfscale.redhat.org",
            ["u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com"],
        )
        assert canonical is None

    def test_ambiguous_single_label_deletion_is_rejected(self):
        canonical, _ = resolve_submitted_host(
            "host.labs.example.com",
            ["host.a.labs.example.com", "host.b.labs.example.com"],
        )
        assert canonical is None

    def test_ip_literals_are_exact_match_only(self):
        canonical, reason = resolve_submitted_host("10.0.0.5", ["10.0.0.5"])
        assert canonical == "10.0.0.5"
        assert reason == "exact"
        canonical, _ = resolve_submitted_host("10.0.0.6", ["10.0.0.5"])
        assert canonical is None


class TestTriageHostIdentityCorrections:
    """The triage completion applies recovery and rejection (#1381)."""

    @staticmethod
    def _run(required_hosts, result, description=_DESCRIPTION):
        from agents.triage.agent import _apply_host_identity_corrections

        ticket = {
            "id": "PERF-TEST",
            "summary": "network throughput on named hosts",
            "description": description,
        }
        return _apply_host_identity_corrections(required_hosts, result, ticket)

    def test_recoverable_transcription_is_corrected_everywhere(self):
        required_hosts = [
            {"roles": ["server"], "host": "u37-h14-000-r7725.labs.perfscale.redhat.com"}
        ]
        result = {
            "parsed_specs": {
                "server_host": "u37-h14-000-r7725.labs.perfscale.redhat.com"
            },
            "scoped_context": {
                "resource": "u37-h14-000-r7725.labs.perfscale.redhat.com"
            },
        }
        error, note = self._run(required_hosts, result)
        assert error is None
        assert "rdu3" in note
        canonical = "u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com"
        assert required_hosts[0]["host"] == canonical
        assert result["parsed_specs"]["server_host"] == canonical
        assert result["scoped_context"]["resource"] == canonical

    def test_unmatched_host_with_near_miss_evidence_rejects(self):
        required_hosts = [
            {
                "roles": ["server"],
                "host": "u37-h14-000-r7725.rdu3.labs.perfscale.example.com",
            }
        ]
        error, note = self._run(required_hosts, {})
        assert error is not None
        assert "verbatim" in error
        assert "u37-h14-000-r7725" in error
        assert note == ""

    def test_host_without_near_miss_evidence_is_left_alone(self):
        # The identity may come from a clarification comment; without
        # related hosts in the text, rejection would be a false positive.
        required_hosts = [{"roles": ["server"], "host": "elsewhere.example.org"}]
        error, note = self._run(required_hosts, {})
        assert error is None
        assert note == ""
        assert required_hosts[0]["host"] == "elsewhere.example.org"

    def test_exact_ip_and_fqdn_pass_untouched(self):
        required_hosts = [
            {"roles": ["client"], "host": "10.0.0.5"},
            {
                "roles": ["server"],
                "host": "u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com",
            },
        ]
        error, note = self._run(required_hosts, {})
        assert error is None
        assert note == ""

    def test_no_host_text_skips_validation(self):
        required_hosts = [{"roles": ["server"], "host": "anything.example.com"}]
        error, note = self._run(
            required_hosts, {}, description="no hosts named here at all"
        )
        assert error is None
        assert note == ""


def test_handoff_failure_names_triage_as_the_source():
    """#1381: the handoff message must blame the stage that supplied the
    bad value, not the resource agent echoing it."""
    ticket = {
        "custom_fields": {
            "resource_provider": "user_provided",
            "required_hosts": [
                {"roles": ["server"], "host": "bad.example.com"},
            ],
            "assigned_hardware_ips": {
                "controller": "good.example.com",
                "targets": ["good2.example.com"],
            },
        }
    }
    from orchestrator.handoff import check_handoff

    ok, reason = check_handoff("awaiting_provision", ticket)
    assert not ok
    assert "triage stage" in reason
    assert "resource agent cannot repair" in reason


def test_comment_authoritative_host_not_rejected():
    """#1381: a host introduced by a user clarification comment must not
    be rejected for not appearing in the description."""
    from agents.triage.agent import _apply_host_identity_corrections

    ticket = {
        "summary": "benchmark",
        "description": "run uperf on host-a.site.example.com",
        "comments": [
            {
                "author": "requester",
                "body": "actually use host-b.site.example.com instead",
            }
        ],
    }
    required = [{"host": "host-b.site.example.com", "role": "client"}]
    rejection, _note = _apply_host_identity_corrections(required, {}, ticket)
    assert rejection is None
    assert required[0]["host"] == "host-b.site.example.com"


def test_system_comment_hosts_are_not_authoritative():
    """#1381: orchestrator/system comment text must not widen the
    authoritative candidate set."""
    from agents.triage.agent import _apply_host_identity_corrections

    ticket = {
        "summary": "benchmark",
        "description": "run uperf on host-a.site.example.com",
        "comments": [
            {
                "author": "orchestrator",
                "body": "retrying with host-c.site.example.com",
            }
        ],
    }
    required = [{"host": "host-c.site.example.com", "role": "client"}]
    rejection, _note = _apply_host_identity_corrections(required, {}, ticket)
    assert rejection is not None
    assert "host-c.site.example.com" in rejection
