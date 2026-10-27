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

**Fix (on the orchestrator host):** remove the stale entry for the changed
identifier and for its other form (hostname or IP). Then collect candidate
keys without trusting them:

Replace both address placeholders with the values from the current
reservation before running these commands.

```bash
CHANGED_HOST='REPLACE_WITH_CHANGED_HOST_OR_IP'
OTHER_HOST='REPLACE_WITH_OTHER_HOSTNAME_OR_IP'
ssh-keygen -R "$CHANGED_HOST"
ssh-keygen -R "$OTHER_HOST"
candidate_keys=$(mktemp)
ssh-keyscan -T 5 -t ecdsa,ed25519 "$CHANGED_HOST" "$OTHER_HOST" > "$candidate_keys"
ssh-keygen -lf "$candidate_keys"
```

Compare each fingerprint against an independently confirmed fingerprint
from the host owner. Only after that comparison succeeds, append the candidate
keys to `~/.ssh/known_hosts` and remove the temporary file:

```bash
cat "$candidate_keys" >> ~/.ssh/known_hosts
rm -f "$candidate_keys"
```

Do not append scanned keys before fingerprint verification.

Also re-establish the trust the workflow needs after a reimage:

- the orchestrator host's key in each lab host's `/root/.ssh/authorized_keys`
- the controller's key in the SUT's `authorized_keys` (crucible SSHes
  controller → SUT). Reuse the existing `~/.ssh/id_ed25519` identity and
  install only its public key. If either key file is missing, report the
  missing path; do not generate a replacement identity.

## 2. Controller firewall blocks crucible coordination

**Symptom:** the run's progress comments stop at a roadblock stage
(`Roadblock: ...:engine-init-begin`), then the run times out. The engine log
on the SUT shows `Error 113 connecting to <controller-ip>:6379. No route to
host.`

**Cause:** fresh lab images ship an iptables INPUT chain that accepts SSH
and rejects everything else (`REJECT --reject-with icmp-host-prohibited`,
which the client sees as "no route to host").

**Fix (on the controller):** allow the crucible coordination ports only from
the assigned SUT IPs, then persist the rules. Set `SUT_IP` to an IP from the
current reservation and repeat the rules for each SUT; do not use an
unrestricted source or a broader subnet:

```bash
SUT_IP='REPLACE_WITH_RESERVED_SUT_IP'
for port in 6379 9200 8000 8001; do
  iptables -I INPUT -p tcp -s "${SUT_IP}/32" --dport "$port" -j ACCEPT
done
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
