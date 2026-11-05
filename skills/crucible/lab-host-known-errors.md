# Lab Host Known Errors

Lab machine allocations are typically wiped between assignments (~4 days).
Several failure modes recur on freshly (re)assigned hosts. This document
describes their symptoms and the agent tools available to address them.

## 1. Changed SSH host keys

**Symptom:** an SSH step reports that the remote host identity has changed.
Reimaging a host can change its host keys; other causes are possible.

**Controller to endpoint:** the benchmark agent can call
`replace_controller_host_key` with the assigned controller and target host.
When the error reports a fingerprint, pass it as `observed_fingerprint` so
the tool selects the key that caused the error.
The tool displays the presented fingerprint and asks the user to approve
replacement. If the ticket explicitly sets `ssh_host_key_policy: replace`,
the tool replaces the changed record without a separate pause. Trusting the
user-provided hostname under this policy also trusts the key presented via
the network path to that hostname. The tool updates only that target's entry
and refuses a key that changes between inspection and replacement.

**Orchestrator to controller:** this tool does not update the orchestrator's
host records. Ask the operator for help if that connection has a changed key.

Also re-establish the trust the workflow needs after a reimage:

- the orchestrator host's key in each lab host's `/root/.ssh/authorized_keys`
- the controller's key in the SUT's `authorized_keys` (crucible SSHes
  controller → SUT). During benchmark pre-run, use the benchmark agent's
  `setup_passwordless_ssh` tool to install the controller's public key.

## 2. Controller firewall blocks crucible coordination

**Symptom:** the run's progress comments stop at a roadblock stage
(`Roadblock: ...:engine-init-begin`), then the run times out. The engine log
on the SUT shows `Error 113 connecting to <controller-ip>:6379. No route to
host.`

**Cause:** fresh lab images ship an iptables INPUT chain that accepts SSH
and rejects everything else (`REJECT --reject-with icmp-host-prohibited`,
which the client sees as "no route to host").

**Fix (on the controller):** allow the crucible coordination ports only from
the assigned SUT IPs and persist the rules. Use an available provisioning
tool that supports the requested source restriction. If no suitable tool is
available, request operator assistance rather than broadening access.

- 6379 = valkey (roadblock coordination — the run dies without it)
- 9200 = opensearch, 8000/8001 = cdm-server

**Preflight before executing:** verify from the SUT with
`test_port_connectivity` (see `skills/general/connectivity-diagnostic.md`)
against the controller's port 6379. These ports only listen while a run is
active, so check reachability (firewall) rather than listening state before
the run.

## 3. Missing crucible identity

**Symptom:** the run-file fails validation with
`email field ... pattern ^.+$` (the run data's email is empty).

**Cause:** crucible sources the run email from the controller's
`/root/.crucible/identity`; a fresh host has no identity file. Harness
verification now warns about this (see `identity_ready` in the
`verify_harness_install` result).

**Fix (on the controller):** have provisioning install the configured
Crucible identity. The private Crucible install contract can supply it. If
the identity is not configured or no suitable tool is available, request
operator assistance.

## 4. Kernel repository trust (approved_repositories)

Kernel A/B tickets may name a `repo_url` for the candidate kernel. The
provisioning tool only writes a dnf repo file when the URL matches an
`approved_repositories` entry in the harness private config
(`~/.agentic-perf/private-skills/crucible.json`):

```json
"approved_repositories": [
  {
    "url_prefix": "https://download.eng.rdu.redhat.com/rhel-9/composes/",
    "gpg_key_secret": "rhel-compose-gpg-key",
    "ca_secret": "rhel-lab-ca-bundle"
  }
]
```

Both secrets must exist in `~/.agentic-perf/secrets/` (or the configured
vault): the compose repository's GPG public key and the TLS CA bundle that
validates the download server's certificate. The tool deploys them to the
host under `/etc/pki/agentic-perf/` and writes the repo with `gpgcheck=1`
and `sslcacert` set — an unauthenticated repository is refused, because
package scriptlets run as root on the benchmark hosts.

**Symptom if unconfigured:** the kernel install step stops with
"repository ... is not in the organization's approved_repositories
configuration". Fix: add the entry + secrets, then retry the transition.
