# Network Connectivity Diagnostic

How to verify and diagnose network connectivity between specific
interfaces on two hosts. No assumptions are made about where
firewalls exist — they could be on the hosts, in cloud security
groups, on intermediate switches, or anywhere in between.

You need SSH access to both hosts.

## Quick connectivity test

Use `nc` (netcat) to test whether a specific IP/port pair is
reachable from a specific source interface.

**Step 1 — start a listener on Host A:**

```bash
# On Host A: listen on a specific IP and port
# Use & to background it so the SSH command returns
nc -l 10.10.0.1 9999 &
```

**Step 2 — probe from Host B:**

```bash
# On Host B: test connectivity, binding to a specific source IP
# -z = connect-then-close (no data), -w 5 = 5 second timeout
nc -z -w 5 -s 10.2.0.1 10.10.0.1 9999
echo $?  # 0 = success, 1 = failed
```

**Step 3 — clean up the listener:**

```bash
# On Host A: kill the backgrounded nc
kill %1 2>/dev/null; pkill -f "nc -l 10.10.0.1 9999" 2>/dev/null
```

**Step 4 — test the reverse direction (B→A may work, A→B may not):**

Repeat with roles swapped. Firewalls are often asymmetric.

If connectivity succeeds in both directions, no further diagnosis
is needed. If it fails, proceed through the layers below.

## Prerequisites check

Before diagnosing connectivity failures, verify the basics on
each host.

**Interface exists and is up:**

```bash
ip link show <interface>
# Look for: state UP, link/ether (has MAC)
```

**IP is assigned to the interface:**

```bash
ip addr show <interface>
# Look for: inet <expected_ip>/<prefix>
```

**Link has carrier (physical/virtual link is connected):**

```bash
cat /sys/class/net/<interface>/carrier
# 1 = link up, 0 = no carrier
```

If any of these fail, fix them before continuing — no amount of
firewall debugging will help if the interface is down or has no IP.

## Layer-by-layer diagnosis

Work through these in order. Stop at the first failure — that
is where the problem is.

### 1. Local firewall on each host

Check each host independently. A firewall on the listener's host
blocks incoming connections. A firewall on the sender's host can
block outgoing connections or return traffic.

```bash
# Check iptables (RHEL 7/8/9, most Linux)
iptables -L -n -v 2>/dev/null | head -30
iptables -L -n -v -t nat 2>/dev/null | head -20

# Check nftables (RHEL 8+, Fedora, Debian 10+)
nft list ruleset 2>/dev/null | head -40

# Check firewalld (if active)
systemctl is-active firewalld
firewall-cmd --list-all 2>/dev/null

# Quick test: are any rules present?
iptables -S 2>/dev/null | grep -v "^-P"  # skip default policies
```

**If rules exist that could block traffic**, the fastest test is
to temporarily flush and retry (only if safe to do so):

```bash
# CAUTION: this removes ALL firewall rules on the host
iptables -F
iptables -X
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT
```

If connectivity works after flushing, the firewall was the cause.
Restore rules and add an appropriate exception rather than leaving
the firewall disabled.

### 2. Routing

Each host needs a route to the other's subnet.

```bash
# Does Host A know how to reach Host B's IP?
ip route get 10.2.0.1
# Look for: "via <gateway>" or "dev <interface>" — not "unreachable"

# Full routing table
ip route show
```

If no route exists, add one:

```bash
ip route add 10.2.0.0/16 via <gateway_ip> dev <interface>
# or for a directly-connected subnet:
ip route add 10.2.0.0/16 dev <interface>
```

### 3. ARP / neighbor resolution (same subnet or next hop)

If the hosts are on the same L2 network (or need to reach a
gateway), they need to resolve each other's MAC address.

```bash
# Check if Host A can ARP for Host B (or its gateway)
arping -c 3 -I <interface> <target_ip>

# View the neighbor table
ip neigh show
# Look for: <ip> dev <iface> lladdr <mac> REACHABLE
# Bad states: FAILED, INCOMPLETE, STALE (with no lladdr)
```

If ARP fails, the hosts cannot reach each other at L2. Possible
causes: wrong VLAN, different broadcast domain, switch port
misconfiguration, or the target host is not connected to the
same network segment.

### 4. Path analysis

If local checks pass but connectivity still fails, something
between the hosts is blocking traffic.

```bash
# Trace the path (ICMP)
traceroute -n <target_ip>

# Or with TCP on the specific port (bypasses ICMP-only firewalls)
traceroute -n -T -p 9999 <target_ip>

# mtr for continuous path analysis
mtr -n --tcp --port 9999 <target_ip>
```

Look for where the path stops responding — the last hop that
replies is the device before the block. The next hop (which
does not reply) is where the firewall or filter exists.

### 5. Port-specific blocking

ICMP (ping) may work while TCP on a specific port is blocked.
This is common with cloud security groups and host firewalls
that allow ping but restrict ports.

```bash
# Does ping work?
ping -c 3 -I <source_ip> <target_ip>

# Does the specific port work?
nc -z -w 5 -s <source_ip> <target_ip> <port>

# If ping works but nc fails, a port-specific firewall rule
# is blocking. Check:
# - Cloud security group / network ACL (AWS, GCP, Azure)
# - Host firewall (iptables, firewalld)
# - Switch ACLs
```

## Common causes quick reference

| Symptom | Likely cause | Check |
|---------|-------------|-------|
| Both directions fail | Host firewall on listener, or no route | `iptables -L -n`, `ip route get` |
| One direction works | Asymmetric firewall rule | Check firewall on the failing direction's listener |
| Ping works, port fails | Port-specific block | Cloud security group, `iptables` port rule |
| Nothing works, not even ping | Interface down, no route, or L2 isolation | `ip link show`, `ip route get`, `arping` |
| Intermittent | MTU mismatch, packet loss, or rate limiting | `ping -s 1400`, `mtr` |
| Works for a while, then stops | Stateful firewall timeout, ARP expiry | Check conntrack, ARP table |

## Resolution patterns

**Host firewall (iptables/nftables/firewalld):**
Disable and mask the service permanently if these are dedicated
test hosts. For production hosts, add a specific rule.
```bash
systemctl stop firewalld nftables iptables 2>/dev/null
systemctl mask firewalld nftables iptables 2>/dev/null
iptables -F; iptables -P INPUT ACCEPT
```

**Cloud security group (AWS):**
Add an inbound rule for the specific port and source IP/CIDR on
the listener's security group. Also verify the VPC's network ACL
allows the traffic (NACLs are stateless — need both inbound and
outbound rules).

**Missing route:**
```bash
ip route add <dest_subnet> via <gateway> dev <interface>
```

**Interface down:**
```bash
ip link set <interface> up
```

**ARP not resolving:**
Verify both hosts are on the same L2 segment. Check VLAN tags,
switch port configuration, and that the target interface is up
with the expected IP.
