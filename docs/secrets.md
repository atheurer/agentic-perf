# Secrets Management

Secrets (API keys, SSH credentials, tokens) are resolved through a
**cascading provider** that checks multiple layers in priority order.
The first layer to contain a requested secret wins; later layers are
skipped (with shadow-detection logging when a duplicate exists).

## Provider Types

### Local (file-backed)

The default. Reads files from `~/.agentic-perf/secrets/`. Each
secret is a file; the request path maps directly to the filesystem:

```
get_secret("aws/config.json")
  → ~/.agentic-perf/secrets/aws/config.json
```

Secrets can be plain text, JSON, or binary (e.g., PKCS12 keystores).
Path-containment checks prevent traversal outside the secrets
directory.

### Bitwarden Secrets Manager (vault)

Optional. Reads secrets from a [Bitwarden Secrets Manager][bsm]
project via the official SDK. Secrets are named with the same
path-shaped keys used by the local provider — a secret named
`aws/config.json` in the configured project serves
`get_secret("aws/config.json")`.

[bsm]: https://bitwarden.com/products/secrets-manager/

Vault secrets are **never persisted to disk** beyond the operation
that needs them. When a consumer requires a file path (e.g., for
SCP), the `secret_file()` context manager materializes an ephemeral
file (mode 0600 in a 0700 tmpdir) and deletes it on exit.

**Requirements:**

- The official Bitwarden Secrets Manager Python SDK:
  `pip install agentic-perf[bitwarden]`
- A machine account access token (see [Bootstrap](#bootstrap-token)
  below)
- A Bitwarden Secrets Manager subscription — **Vaultwarden does not
  support Secrets Manager**. This requires either bitwarden.com
  cloud or an official self-hosted Bitwarden server.

## Cascade Layers

### Single-user mode

Without multi-user mode, the cascade has up to two layers:

| Priority | Label | Source |
|---|---|---|
| 1 | `shared` | `~/.agentic-perf/secrets/` (local files) |
| 2 | `vault:shared` | Bitwarden shared project (if configured) |

A local file always overrides the vault — dropping a file on disk
is the simplest way to override or debug a secret.

### Multi-user mode

With multi-user mode enabled, the cascade expands per-ticket based
on the ticket creator's identity:

| Priority | Label | Source |
|---|---|---|
| 1 | `user:<name>` | `secrets/users/<name>/` |
| 2 | `group:<group>` | `secrets/groups/<group>/` (alphabetical) |
| 3 | `vault:group:<group>` | Bitwarden group project (if configured) |
| 4 | `shared` | `secrets/` (excludes `users/`, `groups/`) |
| 5 | `vault:shared` | Bitwarden shared project (if configured) |

Within each tier, **local beats vault**. A file on disk is an
explicit operator override; vault fills in behind it.

Shadow detection logs when an earlier layer masks a later one, so
administrators can audit which layer served each secret.

## Configuration

Add a `secrets.bitwarden` section to `~/.agentic-perf/config.json`:

```json
{
  "secrets": {
    "bitwarden": {
      "organization_id": "<org-uuid>",
      "shared_project_id": "<project-uuid>",
      "group_project_ids": {
        "gpu-team": "<project-uuid>",
        "network-team": "<project-uuid>"
      },
      "server_url": "https://vault.example.com",
      "cache_ttl_seconds": 60
    }
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `organization_id` | string | yes | Bitwarden organization UUID |
| `shared_project_id` | string | yes | SM project for deployment-shared secrets |
| `group_project_ids` | dict | no | Maps group names to SM project UUIDs |
| `server_url` | string | no | Self-hosted server URL (omit for bitwarden.com cloud) |
| `cache_ttl_seconds` | int | no | TTL for in-memory secret cache (default: 60) |

When this section is absent, vault layers are never constructed and
the system behaves identically to a local-only deployment.

## Bootstrap Token

The Bitwarden SDK requires a machine account access token. This is
the one irreducible bootstrap secret — a vault that needs no
bootstrap credential doesn't exist.

**Resolution order:**

1. Environment variable: `AGENTIC_PERF_BWS_TOKEN`
2. File: `~/.agentic-perf/secrets/bitwarden/access-token` (mode 0600)

The orchestrator resolves the token once at startup. If neither
source is available and vault is configured, startup fails with a
clear error.

```bash
# Option 1: environment variable
export AGENTIC_PERF_BWS_TOKEN="0.xxxxxxxx.yyyyyyyy:zzzzzzzz"

# Option 2: file (recommended for persistent deployments)
mkdir -p ~/.agentic-perf/secrets/bitwarden
echo "0.xxxxxxxx.yyyyyyyy:zzzzzzzz" > ~/.agentic-perf/secrets/bitwarden/access-token
chmod 600 ~/.agentic-perf/secrets/bitwarden/access-token
```

## Caching

The vault provider caches secret values and key listings in memory
with a configurable TTL (default 60 seconds). This avoids a network
round-trip on every secret lookup — the resource registry probes
secret availability per dispatch cycle, and per-probe latency would
be unacceptable.

**Rotation caveat:** a rotated vault secret takes up to
`cache_ttl_seconds` to appear. For immediate rotation visibility,
restart the orchestrator.

An `asyncio.Lock` prevents cache-refresh stampedes when multiple
coroutines request the same secret simultaneously.

## Error Semantics

**Errors are loud; misses are quiet.**

- A vault layer returning "not found" lets the cascade continue
  normally — the next layer is checked.
- A vault layer failing (network error, authentication failure,
  rate limit) raises `SecretsBackendError` and the cascade
  propagates it. The ticket's dispatch fails; the orchestrator
  retries on the next poll cycle.

This is intentional: falling back to shared credentials because the
vault was briefly unreachable is a wrong-credential hazard. The
shadow detection system exists to prevent exactly this class of
silent degradation.

**Missing SDK is the one exception:** if `bitwarden-sdk` is not
installed and vault is configured, a warning is logged and the vault
layer is omitted. This allows the same config.json to work on
machines with and without the optional dependency.

## SSH Key Resolution

SSH keys on tickets are filesystem paths
(`custom_fields.ssh_key_path`). When the file doesn't exist on
disk — because the real key lives in a Bitwarden vault — the
system falls back to vault resolution automatically.

### Configuration

Set `ssh_key_vault_secret` in config.json to the vault secret name:

```json
{
    "ssh_key_vault_secret": "ssh/id_ed25519"
}
```

Per-ticket override: resource agents can set
`custom_fields.ssh_key_secret` on the ticket to use a different
vault secret for a specific machine or provider.

### Resolution Sites

The vault fallback is applied at all SSH key resolution points:

1. **`build_ssh_from_ticket()`** — used by provisioning, resource,
   review, and benchmark MCP servers at subprocess startup.
2. **`set_ssh_context()`** — infra MCP tool called by the LLM.
3. **`_run_selective_teardown()`** — resource agent teardown.
4. **`_run_host_cleanup()`** — resource agent host cleanup.

### Precedence

1. File exists at `ssh_key_path` → use it directly (no vault call)
2. `custom_fields.ssh_key_secret` → per-ticket vault secret name
3. `SSH_KEY_VAULT_SECRET` env var → deployment override
4. `ssh_key_vault_secret` in config.json → global default

### Ephemeral Materialization

When the key is resolved from the vault, `secret_file()` creates
a temporary file (mode 0600 inside a mode 0700 directory) that
exists only for the duration of the SSH operation:

- MCP server subprocesses: key persists for the subprocess
  lifetime via an `AsyncExitStack`.
- Resource agent teardown: key persists for the cleanup block
  via `async with resolve_ssh_key(...)`.

### Error Handling

If a vault secret name is configured but the secret is not found,
`SSHKeyResolutionError` is raised. This is intentional fail-closed
behavior — falling back to no key would let SSH try default
identity files, which could use an unintended credential.

---

## Threat Model

Honest accounting of the security boundaries:

| Aspect | Status |
|---|---|
| Bootstrap token on disk/env | Accepted — irreducible |
| Ephemeral files during SCP | Sub-second exposure, 0600/0700 perms |
| Cache in process memory | Cleared on restart; no disk persistence |
| Vaultwarden compatibility | **Not supported** — Secrets Manager requires official Bitwarden |
| Personal vaults (Phase 2) | Not yet implemented — user cascade tier remains filesystem-only |
| LLM credential exposure | Secrets are injected via MCP tools; the LLM never sees raw values |

## Future: Personal Vault (Phase 2)

The per-user cascade tier (`user:<name>`) currently supports only
local files. A future phase may add personal vault support via the
`bw` CLI (Bitwarden personal vault), allowing individual engineers
to store credentials in their own vaults.

**Design challenge:** the `bw` CLI requires an interactive session
key after unlock. Candidate approaches:

1. **`agentic-perf vault unlock`** — a user-run command that
   authenticates with `bw unlock`, stores the session key with a TTL,
   and makes it available to the orchestrator for that user's tickets.
2. **`bw serve`** — run a local API server per user, with the
   orchestrator connecting to each user's API endpoint.

Both approaches have trade-offs around session lifetime, key storage,
and multi-host deployments. This is deferred to a future planning
session — nothing in the current implementation commits to either
approach, and the provider interface supports either.
