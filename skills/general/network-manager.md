# NetworkManager Interface Configuration

## Why `ip link` and `ethtool` changes don't persist

NetworkManager owns network interface configuration on RHEL/Fedora/CentOS.
When NM manages an interface, any changes made outside NM (via `ip link set`,
`ip addr add`, etc.) are overridden the next time NM processes a connection
event — on reconnect, after a brief link-down, or at boot.

**Always configure persistent settings via NM tools (`nm_set_mtu`,
`nm_set_ip`), not `ip link` or `ip addr`.**

The NM tools modify the connection profile (stored in
`/etc/NetworkManager/system-connections/`) and bring the connection up,
making the change both immediate and persistent.

## MTU Configuration

### The right way
```
nm_set_mtu(host, interface="eno16695np0", mtu=9000)
```
This runs:
```bash
nmcli connection modify '<profile>' 802-3-ethernet.mtu 9000
nmcli connection up '<profile>'
```

### The wrong way
```bash
ip link set eno16695np0 mtu 9000  # reverts on next NM event
```

### MTU end-to-end requirements

**Setting MTU 9000 on the interface is necessary but not sufficient.**
Every Layer 2 hop in the path must support the larger frame size:
- Both NIC drivers must accept the MTU (nearly all modern NICs do)
- Every switch port carrying the traffic must be configured for jumbo frames
- Every VLAN trunk between switches must support the larger frame
- If ICMP is blocked (common in datacenters), PMTUD cannot negotiate down
  silently — oversized frames may be dropped without any visible error

**Do not recommend MTU 9000 as a general tuning parameter.** Only set it
when the user explicitly requests jumbo frames AND the network path is known
to support them. Verify with:
```bash
ping -c 3 -M do -s 8972 <remote_ip>
# 8972 = 9000 - 20 (IP header) - 8 (ICMP header)
```
A successful response without fragmentation confirms end-to-end support.

### MTU and GRO/GSO

A common assumption is that jumbo frames dramatically reduce per-packet CPU
cost. This is less true when GRO and GSO are enabled (the RHEL default):

- **GRO (Generic Receive Offload)**: the kernel coalesces multiple incoming
  wire-MTU frames into a single large sk_buff (up to 64KB) before passing to
  the TCP stack. At 1500 MTU with GRO, the effective unit of processing is
  already 32–40KB — close to a 9000-byte jumbo frame.
- **GSO (Generic Segmentation Offload)**: on transmit, the kernel works with
  large buffers and lets the NIC segment them, avoiding per-segment CPU work.
- **Interrupt coalescing** (`ethtool -C`) further reduces interrupt rate
  independently of MTU.

The result: the measurable throughput difference between 1500 and 9000 MTU
on a GRO/GSO-enabled stack is real but narrower than raw packet-count math
suggests. Always measure both to characterize the actual impact for your
workload.

## IP Address Configuration

### Static IP
```
nm_set_ip(host, interface="eno16695np0",
          ip_cidr="172.16.0.1/24", gateway=None)
```

### DHCP
```
nm_set_dhcp(host, interface="eno16695np0")
```

### Private test network pattern

When a ticket requests a dedicated test network for benchmark traffic
(e.g., "configure a private 172.16.x.x network on eno16695np0"):

1. Call `nm_set_ip` on each endpoint with addresses in the same subnet
2. Call `nm_verify_interface` to confirm the IP is live and the interface is UP
3. Optionally call `nm_set_mtu` if a specific MTU is requested
4. Include the IP and MTU in the provisioning report so the benchmark agent
   knows which address to use for test traffic

Example for two endpoints:
```
nm_set_ip(host=client, interface="eno16695np0", ip_cidr="172.16.0.1/24")
nm_set_ip(host=server, interface="eno16695np0", ip_cidr="172.16.0.2/24")
nm_verify_interface(host=client, interface="eno16695np0",
                    expected_ip="172.16.0.1")
nm_verify_interface(host=server, interface="eno16695np0",
                    expected_ip="172.16.0.2")
```

## Auditing interface state

Always call `nm_show_connection` before a benchmark to record the actual
MTU and IP in the provisioning report. This ensures the review agent can
verify that results were obtained under the intended configuration.
