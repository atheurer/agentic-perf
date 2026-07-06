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
   This uses the IP set in the `remotehost` mv-param.
   Crucible's uperf server listens on specific TCP ports,
   so ping or SSH connectivity does NOT prove this works.

**You must verify layer 3 before constructing the run-file.**
Layers 1 and 2 may use different IPs than layer 3,
especially in cloud environments where hosts have multiple
interfaces or IP addresses.

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

### Choosing the remotehost IP

When hosts have multiple IPs (common in cloud — public IP,
private management IP, possibly additional private IPs on
different subnets), you must pick the IP pair where the
uperf ports are reachable:

1. List candidate IPs on both client and server hosts.
2. For each candidate server IP, test port connectivity
   from the client using `test_port_connectivity` or
   manual `nc` — test on the actual uperf ports (30002
   and 30003 for a single pair), not just ping.
3. Use the server IP that passes the port test as the
   `remotehost` value.

See `general/connectivity-diagnostic.md` for the full
connectivity testing procedure.

**Do NOT assume** that the management/SSH IP is the right
choice for `remotehost`. In cloud environments, the private
subnet IPs often work while public IPs have security group
rules blocking the uperf port range.

### Discovering test interfaces

When the user specifies non-management NICs, you need to
discover what's available on the hosts. Use `execute_command`
to run these on the client and server hosts:

```
ip -br addr show | grep UP
```

This shows all UP interfaces with their IPs. Look for:
- Interfaces with IPs on a shared private subnet (e.g.,
  10.10.x.x on both hosts) — these are likely the test
  network
- Interfaces with only link-local IPv6 (fe80::) — these
  are UP but have no IPv4 address configured

You can also identify NIC types:
```
ethtool -i <interface> | grep driver
ethtool <interface> | grep Speed
```

### Choosing remotehost vs ifname

Once you know the test interfaces:

- If the test NICs have IPv4 addresses on a shared subnet,
  use `remotehost` with the server's test-network IP (not
  the management IP).
- If you want to specify the interface by name, use
  `ifname` with the server's interface name. The
  uperf-server-start script resolves the IP from the
  interface automatically.
- If the test NICs have no IP addresses, you cannot
  proceed without network configuration. Use
  `request_clarification` to tell the user which
  interfaces you found and ask how to proceed.

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

The client must know where the server is. Use one of:

- **`remotehost`** (role: client) — hostname or IP of the
  server host. Use this when you know the server's address.
  When testing specific NICs, use the IP on the test network,
  not the management IP.
- **`ifname`** (role: server) — network interface name on
  the server. The server-start script finds the IP from the
  interface and sends it to the client via roadblock. Use
  this when the server has multiple interfaces and you want
  to target a specific one.

Use ONE of these, not both — they are mutually exclusive.
For most cases, use `remotehost` with the server's hostname
or IP address. When testing specific NICs, prefer
`remotehost` with the test-network IP if available, or
`ifname` with the server's interface name.

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
        {"arg": "remotehost", "vals": ["<server-hostname-or-ip>"], "role": "client"}
      ]
    }
  ]
}
```

## Engine IDs

Client and server must share the same engine ID (see
run-file-pitfalls.md). For a single client-server pair:

```json
"remotes": [
  {
    "engines": [{"role": "client", "ids": ["1"]}],
    "config": {"host": "<client-ip>", "settings": {"userenv": "fedora-latest", "osruntime": "podman"}}
  },
  {
    "engines": [{"role": "server", "ids": ["1"]}],
    "config": {"host": "<server-ip>", "settings": {"userenv": "fedora-latest", "osruntime": "podman"}}
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
- **remotehost**: any hostname or IP
- **ifname**: any network interface name
