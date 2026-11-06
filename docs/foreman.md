# Foreman service configuration

Foreman is configured as the organization service subject `service/foreman`
using the existing skill gateway's organization Git or local-path sources. In a
repository hierarchy the document is `service-config/service/foreman.json`.
The installation-local secret `foreman/config.json`, if present, replaces the
entire organization document; settings from the two sources are not merged.
Invalid local configuration fails instead of falling back. The site must be
specified when more than one is configured. A local installation file is not
user-scoped isolation; authenticated user-scoped configuration depends on the
context gateway's future identity and isolation support.

Example service document (IDs are deployment-specific examples, not defaults):

```json
{
  "sites": {
    "lab-a": {
      "endpoint": "https://foreman.example.org",
      "ca_bundle": "/etc/pki/ca-trust/source/anchors/lab-ca.pem",
      "auth": {
        "strategy": "secret",
        "username_secret": "foreman/lab-a-username",
        "password_secret": "foreman/lab-a-password"
      },
      "bootstrap": {"mode": "existing"},
      "os_options": {
        "RHEL 10.0": {
          "medium_id": 15,
          "ptable_id": 169,
          "os_release_id": "rhel",
          "os_release_version": "10.0"
        }
      }
    }
  }
}
```

Use secret references, never credential values, in the document. HTTPS server
verification is mandatory; `ca_bundle` is optional and defaults to system trust.
The endpoint must not contain credentials. `os_options` are preferences for
installation media and partition tables, not an OS availability catalog; the
live OS and its architecture and options must be revalidated against Foreman.
Option IDs can be omitted when the live OS exposes exactly one choice; multiple
choices require explicit preferences. No OS is inferred from a major version
when several releases match. The optional `os_release_id` and
`os_release_version` specify the expected `/etc/os-release` identity for
post-install verification when it cannot be derived from Foreman details.

Every site must choose an SSH bootstrap policy. `{"mode":"existing"}` uses
existing SSH access and does not replace host keys or install keys. QUADS sites
may explicitly opt into
`{"mode":"quads","host_key_policy":"replace-unverified"}`. This clears the
old known-host entry, accepts an unauthenticated new host key, and uses the
site-managed QUADS password to install the existing configured SSH public key.
Use this only where that host-key trust model is acceptable. It does not create
an SSH key pair. Missing identity files stop the operation. A key added by this
workflow is tagged with the QUADS provisioning marker and removed during ticket
cleanup; a key that was already present is preserved.

For QUADS deployments that have independently confirmed their credential
convention, `auth` may instead be:

```json
{"strategy": "quads", "infra_location": "lab-a", "convention_verified": true}
```

The adapter derives credentials in the service from the current assignment's
cloud and ticket, and checks the active assignment and each target's current
cloud. A QUADS assignment ID and an agentic-perf ticket ID are not the QUADS
assignment ticket. Configured site, allocation identity, and current ownership
must all agree before any destructive operation.

The resource tool `discover_quads_os_catalog` returns live pre-allocation names,
the configured QUADS site, and discovery time. A lookup failure is reported as
unknown; a successful empty list is distinct. This catalog is useful for planning
initial reservations, but is not proof of Foreman compatibility for an allocated
host. Foreman reinstall intent is separate from the QUADS reservation OS.
Selective roles across multiple hosts are not supported until a verified
role-to-host mapping is available; such requests fail closed. Formal
(council-allocated) QUADS assignments can be rebuilt through a user-authored
`quads` directive naming the assignment's cloud and ticket. That path is
limited to hosts named in `required_hosts` and to `quads` or `user_provided`
tickets; the directive identity outranks stale reservation metadata. Because a
formal assignment has no local assignment ID, ownership is verified against the
active QUADS assignment for the directive's cloud (including its ticket and
validated state) and each target host's current cloud before every destructive
step. Managed QUADS tickets keep their assignment ID even when a directive is
present, so the active assignment must also match the directive. Other
non-QUADS hosts remain unsupported because their authorization contract is not
implemented, although the client is reusable.

In multi-user mode, checkpoint updates require service authentication and an
active platform dispatch claim. Legacy deployments use a shared service token:
anyone holding it is trusted as the service and can impersonate a claim, so it
must not be shared with untrusted ticket writers. Authenticated user-scoped
service configuration remains unsupported.

Live integration testing must be explicitly opted into on an allocated test host, and should
record allocation, requested OS, site, selected options, host verification,
and cleanup without recording credentials. Never use a production host.
