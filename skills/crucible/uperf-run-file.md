# uperf Run-File Construction Guide

uperf is a client-server network benchmark. The client
generates traffic to the server and measures throughput
and latency.

## Connectivity — three distinct layers

uperf requires the client and server to communicate over a
network path. Before constructing the run-file, understand
that there are three separate connectivity layers — each
with different requirements, and testing one does NOT
guarantee the others work:

1. **Agentic-perf → hosts (SSH):** How the automation
   platform reaches the hosts. Uses `ssh_hardware_ips`
   (often public IPs in cloud). This is already verified
   by the resource agent before you receive the ticket.

2. **Crucible controller → remotes (SSH):** How the
   crucible controller orchestrates the endpoints. Uses
   the IPs in the `host` field of each remote in the
   run-file's `endpoints` section. These must be IPs the
   controller can SSH to as root — typically the private
   IPs in `assigned_hardware_ips`.

3. **Benchmark data-plane (uperf ports):** How the uperf
   client reaches the uperf server during the actual test.
   The server binds to the IP of the `ifname` interface and
   sends that IP to the client via roadblock. Crucible's
   uperf server listens on specific TCP ports, so ping or
   SSH connectivity does NOT prove this works.

**You must verify layer 3 before constructing the run-file.**
Layers 1 and 2 may use different IPs than layer 3,
especially in cloud environments where hosts have multiple
interfaces or IP addresses.

**Also verify layer 2 before calling `execute_benchmark`** —
don't just trust whatever value ended up in each endpoint's
`host` field. SSH access from the controller to each endpoint
should already be in place from `setup_passwordless_ssh`, so
this is a real connectivity check, not a port probe. Call
`verify_ssh_path(controller_host, endpoint_host)` for each
endpoint host in your run-file.

If this fails for a host that `setup_passwordless_ssh` already
reported as reachable, you likely put the wrong value in that
endpoint's `host` field (e.g. a layer-3 test-interface IP
instead of the layer-2 access address) — see "Discovering test
interfaces" below.

### uperf port formula

Crucible's uperf benchmark uses these TCP ports per
client-server instance (called a "csid"):

- **Control port:** `30000 + 2 * N`
- **Data port:** `30000 + 2 * N + 1`

Where N is the csid. For a single client-server pair, the
default csid is 1, so the ports are **30002** (control) and
**30003** (data).

For multiple pairs, pair 2 uses 30004/30005, pair 3 uses
30006/30007, etc.

### Discovering test interfaces

When the user specifies non-management NICs, you need to
discover what's available on the hosts. Call
`get_hardware_topology(host)` (or `list_interfaces(host)`) on each of the
client and server hosts. `get_hardware_topology` returns all network interfaces
in `netdevs` with their operstate, driver, link speed, MAC, NUMA node, PCI address,
and assigned IP addresses in a single call. Look for:
- Interfaces with IPs on a shared private subnet (e.g.,
  10.10.x.x on both hosts) — these are likely the test
  network
- Interfaces with only link-local IPv6 (fe80::) — these
  are UP but have no IPv4 address configured

**Don't assume the IP you find here is for the endpoint `host`
field (layer 2, above) — you likely already have what you need
for that.** This discovery is for identifying the test NIC and
confirming layer 3 connectivity (`test_port_connectivity`,
`ifname`). It's technically possible for the test IP to also be
the access IP, but don't default to that: check what's already
in `assigned_hardware_ips` for the endpoint `host` field before
reaching for whatever you just discovered here.

You can also pass `jq_filter` directly in `get_hardware_topology` to slice
active interfaces (e.g., `jq_filter=".netdevs | to_entries[] | select(.value.operstate == \"up\")"`).

### Choosing remotehost vs ifname

**Use `ifname` on the server.  Do NOT use `remotehost`.**

How the roadblock mechanism works:
1. Server: `ifname=ens1f0np0` → uperf-server-start resolves
   the IP of that interface (e.g. `172.18.38.68`), starts
   uperf listening on it, and sends that IP to the client via
   crucible roadblock messaging at `server-start-end`.
2. Client: reads the server IP from the roadblock message
   automatically. No `remotehost` param is needed or wanted.

**Never set `remotehost` in mv-params when `ifname` is used.**
If `remotehost` is present, crucible injects it directly into
the client bench-start-cmds, bypassing the roadblock IP
exchange entirely. The hostname in `remotehost` almost always
resolves to the management NIC (not the test NIC), so all
traffic flows over the wrong interface at a fraction of the
intended speed.

If the test NICs have no IP addresses, use
`request_clarification` to tell the user which interfaces you
found and ask how to proceed.

### Matching user intent to interfaces

The user may describe NICs in various ways:
- By speed: "25G NICs", "100G interfaces"
- By vendor: "Intel NICs", "ConnectX-7", "Mellanox"
- By driver: "i40e", "mlx5", "ice"
- By name: "ens2f0", "eno16495np0"
- By purpose: "the test NICs", "not the management interface"

Use `ethtool -i` and `ethtool` to match the user's
description to actual interface names on the hosts.

## Required mv-params

Use **`ifname`** (role: server) to select the test NIC.
Do NOT use `remotehost` — the server sends its resolved IP
to the client via roadblock automatically.

- **`ifname`** (role: server) — the network interface name
  on the server host (e.g. `ens1f0np0`). uperf-server-start
  resolves the IP of that interface and passes it to the
  client via roadblock. This is the only correct way to
  target a specific NIC.
- **`remotehost`** — do not use. It bypasses the roadblock
  mechanism and will cause traffic to flow on the wrong NIC.

## Typical uperf mv-params

```json
"mv-params": {
  "sets": [
    {
      "params": [
        {"arg": "test-type", "vals": ["stream"], "role": "client"},
        {"arg": "protocol", "vals": ["tcp"], "role": "client"},
        {"arg": "wsize", "vals": ["16384"], "role": "client"},
        {"arg": "duration", "vals": ["30"], "role": "client"},
        {"arg": "nthreads", "vals": ["1"], "role": "client"},
        {"arg": "ifname", "vals": ["ens1f0np0"], "role": "server"}
      ]
    }
  ]
}
```

Note: there is no `remotehost` param. The server resolves
the IP of `ifname` and sends it to the client via roadblock.

## Engine IDs

Client and server must share the same engine ID (see
run-file-pitfalls.md). For a single client-server pair:

```json
"remotes": [
  {
    "engines": [{"role": "client", "ids": ["1"]}],
    "config": {"host": "<client-ip>", "settings": {"userenv": "<see userenv-guide>", "osruntime": "podman"}}
  },
  {
    "engines": [{"role": "server", "ids": ["1"]}],
    "config": {"host": "<server-ip>", "settings": {"userenv": "<see userenv-guide>", "osruntime": "podman"}}
  }
]
```

Benchmark section references the same ID:
```json
"benchmarks": [{"name": "uperf", "ids": "1", "mv-params": {...}}]
```

## Valid parameter values

From multiplex.json:
- **test-type**: stream, crr, rr, ping-pong
- **protocol**: tcp, udp
- **ipv**: 4, 6
- **wsize/rsize/nthreads/duration**: positive integers
- **ifname**: network interface name on the server (use this, not remotehost)
