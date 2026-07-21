# Multi-User Mode

Multi-user mode adds cooperative access control to agentic-perf.
Each engineer gets a personal API token, tickets track ownership,
and secrets can be scoped per-user. It is designed for shared lab
environments where multiple engineers submit work through the same
deployment.

## Enabling Multi-User Mode

Add to `~/.agentic-perf/config.json`:

```json
{
  "auth": {
    "multi_user": true
  }
}
```

Restart the state store and orchestrator after changing this setting.

When `auth.multi_user` is `false` (the default), the system behaves
identically to before: a single deployment token authenticates all
requests, there is no ownership tracking, and all secrets are shared.

## Concepts

### Principals

Every authenticated request carries a **principal** — the identity
behind the request.

| Kind | Source | Capabilities |
|---|---|---|
| **service** | Deployment token (`AGENTIC_PERF_API_TOKEN`) | Full access to all tickets. Used by agents and automation. |
| **user** | Per-user bearer token | Scoped to owned tickets. Can be admin. |

### Users and Tokens

Users are created by an admin (or bootstrap with the deployment token):

```bash
agentic-perf user create alice
# Token: abc123... (shown once — save it)

export AGENTIC_PERF_API_TOKEN=abc123...
agentic-perf whoami
# alice (user)
```

Tokens are SHA-256 hashed before storage. The raw token is shown
exactly once at creation time. If lost, rotate it:

```bash
agentic-perf user rotate-token alice
```

### Groups

Groups organize users for shared secret access:

```bash
agentic-perf group create gpu-team
agentic-perf group add-member gpu-team alice
agentic-perf group add-member gpu-team bob
```

Groups currently affect only secrets resolution (see below).

### Ticket Ownership

When a user creates a ticket, they become the **creator** and are
automatically added to the **owners** list. Owners control who can:

- Update custom fields
- Add comments
- Trigger transitions
- Stop or abort the ticket

Non-owners receive a 403 response. Admins and service principals
bypass ownership checks.

#### Handoff

Transfer ownership to another engineer:

```bash
agentic-perf handoff TICKET-001 --add bob
agentic-perf handoff TICKET-001 --remove alice
```

The last owner cannot be removed (409 error). This prevents
orphaned tickets that nobody can manage.

#### Claiming Unclaimed Tickets

Service-created tickets start with no owners. Any user can claim
one by adding themselves:

```bash
agentic-perf claim TICKET-001
```

This self-add carve-out only works on unclaimed tickets. Once a
ticket has owners, only current owners or admins can add more.

### Per-User Secrets

When multi-user mode is enabled, secrets resolve through a cascade:

1. `~/.agentic-perf/secrets/users/<username>/` — user-private
2. `~/.agentic-perf/secrets/groups/<group>/` — group-shared (alphabetical)
3. `~/.agentic-perf/secrets/` — deployment-shared

The first layer that contains the requested secret wins. Shadow
detection logs when an earlier layer masks a later one, so
administrators can audit overrides.

**The ticket creator determines the secrets cascade.** When a user
submits a ticket, agents processing that ticket use the creator's
cascade. This means:

- Alice's AWS credentials are only used for Alice's tickets
- A shared Crucible token in `secrets/` is available to everyone
- If Alice overrides the Crucible token in `secrets/users/alice/`,
  only her tickets use the override

Unclaimed tickets (created by service principals) use the shared
deployment secrets.

## Bootstrap Walkthrough

```bash
# 1. Enable multi-user mode
cat > ~/.agentic-perf/config.json <<'EOF'
{"auth": {"multi_user": true}}
EOF

# 2. Restart services
./scripts/start-bg.sh stop
./scripts/start-bg.sh

# 3. Create users (using deployment token)
agentic-perf user create alice --admin
agentic-perf user create bob

# 4. Set up per-user secrets (optional)
mkdir -p ~/.agentic-perf/secrets/users/alice/aws
cp /path/to/alice-credentials ~/.agentic-perf/secrets/users/alice/aws/credentials

# 5. Distribute tokens to engineers
# Each engineer sets: export AGENTIC_PERF_API_TOKEN=<their-token>
```

## Dashboard Token Management

In multi-user mode, the dashboard does **not** embed the deployment
token. Instead, engineers paste their personal token via the
settings gear icon in the header:

1. Click the gear icon
2. Paste your API token
3. Click Save — the token is validated against `/whoami`
4. Your identity appears in the header

The token is stored in `localStorage` and persists across browser
sessions. Click Clear to remove it.

Write controls (Stop, Pause, Abort) are disabled when you are not
an owner of the ticket you're viewing.

## Security Model

Multi-user mode provides **cooperative access control**, not tenant
isolation. It prevents accidental cross-user interference in a
shared lab — it does not defend against a malicious user with
network access to the state store.

**What is enforced:**

- Per-user bearer tokens (SHA-256 hashed, never stored in plaintext)
- Ownership-based write gating on all mutating ticket endpoints
- Admin-only user/group management
- Per-user secrets cascade with per-layer path containment
- Service principal bypass for agents (they need full access)

**What is NOT enforced:**

- The state store binds to `0.0.0.0` — anyone on the network can
  reach it. Place behind a firewall on untrusted networks.
- Tokens are shared deployment secrets, not per-session credentials.
  There is no token expiry, refresh, or revocation log.
- Users can read all tickets (only writes are gated). Read isolation
  is not a goal.
- The secrets cascade trusts filesystem permissions. A user with
  shell access to the host can read any secret directory.
- Command execution on remote hosts runs as root over SSH with no
  per-user confinement.

For environments requiring stronger isolation, consider running
separate deployments per team rather than relying on multi-user mode.
