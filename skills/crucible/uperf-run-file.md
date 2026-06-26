# uperf Run-File Construction Guide

uperf is a client-server network benchmark. The client
generates traffic to the server and measures throughput
and latency.

## Network connectivity

uperf requires the client and server to communicate over
a network path. Which network path depends on what the
user wants to test:

- **Management network (default):** The IPs in
  `assigned_hardware_ips` are management addresses. Using
  these tests the default network path — typically the
  onboard NIC or whatever interface carries SSH traffic.
- **Specific NICs / test network:** When the user asks to
  test specific NICs (e.g., "25G NICs", "ConnectX-7",
  "the high-speed interfaces"), the benchmark traffic must
  flow over those NICs, not the management network. This
  requires knowing the interface names or IPs on the test
  network.

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
