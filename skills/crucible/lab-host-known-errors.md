# Lab Host Known Errors

Lab machine allocations are typically wiped between assignments (~4 days).
Three failure modes recur on freshly (re)assigned hosts. Each one currently
surfaces far from its cause; this doc gives the symptom, the root cause, and
the fix so agents and operators can resolve them in one step.

## 1. Changed SSH host keys

**Symptom:** any SSH step fails with `REMOTE HOST IDENTIFICATION HAS
CHANGED` (the kernel/provisioning tools annotate this error with the recipe
below).

**Cause:** the host was reimaged and presents new keys.

**Fix (on the orchestrator host):** refresh BOTH forms of the address —
tools connect by resolved IP, humans test with hostnames:

```bash
ssh-keygen -R <hostname> ; ssh-keygen -R <ip>
ssh-keyscan -T 5 -t ecdsa,ed25519 <hostname> <ip> >> ~/.ssh/known_hosts
```

Verify the new key fingerprint with the host owner before accepting it.

Also re-establish the trust the workflow needs after a reimage:

- the orchestrator host's key in each lab host's `/root/.ssh/authorized_keys`
- the controller's key in the SUT's `authorized_keys` (crucible SSHes
  controller → SUT; a fresh controller has no keypair at all — generate one
  with `ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519`)

## 2. Controller firewall blocks crucible coordination

**Symptom:** the run's progress comments stop at a roadblock stage
(`Roadblock: ...:engine-init-begin`), then the run times out. The engine log
on the SUT shows `Error 113 connecting to <controller-ip>:6379. No route to
host.`

**Cause:** fresh lab images ship an iptables INPUT chain that accepts SSH
and rejects everything else (`REJECT --reject-with icmp-host-prohibited`,
which the client sees as "no route to host").

**Fix (on the controller):** allow the crucible coordination ports and
persist the rules:

```bash
for port in 6379 9200 8000 8001; do iptables -I INPUT -p tcp --dport $port -j ACCEPT; done
iptables-save > /etc/sysconfig/iptables
```

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

**Fix (on the controller):**

```bash
printf 'CRUCIBLE_NAME="<full name>"\nCRUCIBLE_EMAIL="<email>"\n' > /root/.crucible/identity
```

The private crucible config (`~/.agentic-perf/private-skills/crucible.json`)
can carry this in its install contract so provisioning deploys it
automatically.
