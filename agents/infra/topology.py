"""Hardware topology and cache discovery library.

Discovers CPU cache topology (CCD/LLC domains, L3 cache slices, core mappings,
and NUMA locality) across AMD, Intel, ARM, and other architectures.

Provides structured CCD maps for cache-aware IRQ pinning and CPU affinity:
  {0: [192, 193, ..., 207, 576, 577, ..., 591],
   1: [208, 209, ..., 223, 592, ..., 607],
   ...}
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from providers.ssh import SSHExecutor

logger = logging.getLogger(__name__)


def parse_cpu_list(text: str | None) -> list[int]:
    """Parse a Linux CPU list string (e.g. '0-3,8,10-12') into a sorted list of CPU integers."""
    if text is None:
        return []
    s = str(text).strip()
    if not s:
        return []
    cpus: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start_s, end_s = part.split("-", 1)
                start, end = int(start_s.strip()), int(end_s.strip())
                if start <= end:
                    cpus.update(range(start, end + 1))
            except ValueError:
                continue
        else:
            try:
                cpus.add(int(part))
            except ValueError:
                continue
    return sorted(cpus)


def format_cpu_list(cpus: Iterable[int]) -> str:
    """Format an iterable of CPU integers into a compact Linux CPU list string (e.g. '0-3,8,10-12')."""
    sorted_cpus = sorted(set(cpus))
    if not sorted_cpus:
        return ""
    ranges: list[str] = []
    start = sorted_cpus[0]
    end = start
    for c in sorted_cpus[1:]:
        if c == end + 1:
            end = c
        else:
            ranges.append(f"{start}-{end}" if start != end else f"{start}")
            start = end = c
    ranges.append(f"{start}-{end}" if start != end else f"{start}")
    return ",".join(ranges)


def parse_cpu_mask(mask: str | None) -> list[int]:
    """Parse a Linux cpumask hex string (e.g. '00000001,00000003' or 'f') into a sorted CPU list."""
    if not mask:
        return []
    clean = str(mask).strip().replace(" ", "")
    if not clean or clean == "0":
        return []
    parts = clean.split(",")
    cpus: list[int] = []
    for i, word in enumerate(reversed(parts)):
        word = word.strip()
        if not word:
            continue
        try:
            val = int(word, 16)
        except ValueError:
            continue
        base = i * 32
        bit = 0
        while val > 0:
            if val & 1:
                cpus.append(base + bit)
            val >>= 1
            bit += 1
    return sorted(cpus)


@dataclass
class CacheDomain:
    """Represents a single cache slice / CCD domain."""

    ccd_id: int
    cache_id: int | None
    socket_id: int
    numa_node: int | None
    cpus: list[int]
    cpu_list: str
    core_count: int
    thread_count: int
    size: str | None = None
    level: int = 3
    type: str | None = "Unified"
    die_id: int | None = None
    cluster_id: int | None = None
    source: str = "sysfs"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CacheTopologyResult:
    """Structured cache topology discovery result."""

    host: str
    socket: int | None
    ccds: dict[int, list[int]]
    domains: list[CacheDomain]
    total_ccds: int
    total_cpus: int
    cache_level: int = 3
    source: str = "sysfs"
    vendor: str | None = None
    model: str | None = None
    architecture: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "host": self.host,
            "socket": self.socket,
            "cache_level": self.cache_level,
            "source": self.source,
            "ccds": {str(k): v for k, v in self.ccds.items()},
            "domains": [dom.to_dict() for dom in self.domains],
            "total_ccds": self.total_ccds,
            "total_cpus": self.total_cpus,
        }
        if self.vendor:
            d["vendor"] = self.vendor
        if self.model:
            d["model"] = self.model
        if self.architecture:
            d["architecture"] = self.architecture
        if self.error:
            d["error"] = self.error
        return d


_TOPOLOGY_COLLECTOR_SCRIPT = r"""
import glob, os, json, subprocess, sys

def collect():
    data = {'cpus': {}, 'nodes': {}, 'system': {}, 'cpuinfo': []}
    try:
        data['system']['arch'] = os.uname().machine
        data['system']['kernel'] = os.uname().release
    except Exception:
        pass

    # Read /proc/cpuinfo
    try:
        if os.path.exists('/proc/cpuinfo'):
            cur = {}
            with open('/proc/cpuinfo') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        if cur:
                            data['cpuinfo'].append(cur)
                            cur = {}
                        continue
                    if ':' in line:
                        k, v = line.split(':', 1)
                        k = k.strip()
                        v = v.strip()
                        cur[k] = v
                if cur:
                    data['cpuinfo'].append(cur)
            if data['cpuinfo']:
                first = data['cpuinfo'][0]
                data['system']['vendor'] = first.get('vendor_id')
                data['system']['model'] = first.get('model name')
    except Exception:
        pass

    # NUMA nodes from sysfs
    try:
        for np in sorted(glob.glob('/sys/devices/system/node/node[0-9]*')):
            node_name = os.path.basename(np)
            node_id = int(node_name.replace('node', ''))
            cpulist_path = os.path.join(np, 'cpulist')
            if os.path.exists(cpulist_path):
                with open(cpulist_path) as f:
                    data['nodes'][node_id] = f.read().strip()
    except Exception:
        pass

    # CPUs from sysfs
    try:
        for cp in sorted(glob.glob('/sys/devices/system/cpu/cpu[0-9]*')):
            cname = os.path.basename(cp)
            if not cname[3:].isdigit():
                continue
            cpu_id = int(cname[3:])
            info = {'cpu_id': cpu_id}

            # online
            online_p = os.path.join(cp, 'online')
            if os.path.exists(online_p):
                try:
                    with open(online_p) as f:
                        info['online'] = (f.read().strip() == '1')
                except Exception:
                    info['online'] = True
            else:
                info['online'] = True

            # topology
            top_p = os.path.join(cp, 'topology')
            if os.path.exists(top_p):
                for field in ['physical_package_id', 'core_id', 'die_id', 'cluster_id', 'thread_siblings_list', 'core_cpus_list']:
                    fp = os.path.join(top_p, field)
                    if os.path.exists(fp):
                        try:
                            val = open(fp).read().strip()
                            info[field] = int(val) if (val.isdigit() or (val.startswith('-') and val[1:].isdigit())) else val
                        except Exception:
                            pass

            # cache
            caches = []
            for cidx in sorted(glob.glob(os.path.join(cp, 'cache/index*'))):
                cinfo = {}
                for field in ['level', 'type', 'id', 'size', 'shared_cpu_list', 'shared_cpu_map']:
                    fp = os.path.join(cidx, field)
                    if os.path.exists(fp):
                        try:
                            val = open(fp).read().strip()
                            cinfo[field] = int(val) if (field in ('level', 'id') and (val.isdigit() or (val.startswith('-') and val[1:].isdigit()))) else val
                        except Exception:
                            pass
                if cinfo:
                    caches.append(cinfo)
            info['caches'] = caches
            data['cpus'][cpu_id] = info
    except Exception:
        pass

    # Fallback to lscpu if sysfs returned nothing
    if not data['cpus']:
        try:
            res = subprocess.run(['lscpu', '-e', '-J'], capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                data['lscpu'] = json.loads(res.stdout).get('cpus', [])
        except Exception:
            pass

    print(json.dumps(data))

if __name__ == '__main__':
    collect()
"""


def _find_numa_node_for_cpu(cpu_id: int, nodes: dict[int, str]) -> int | None:
    """Find NUMA node ID that contains cpu_id using node cpulists."""
    for node_id, cpulist_str in nodes.items():
        if cpu_id in parse_cpu_list(cpulist_str):
            return int(node_id)
    return None


def parse_topology_data(
    data: dict[str, Any],
    host: str = "localhost",
    socket: int | None = None,
) -> CacheTopologyResult:
    """Parse raw collected topology data dictionary into CacheTopologyResult."""
    system_info = data.get("system", {})
    vendor = system_info.get("vendor")
    model = system_info.get("model")
    architecture = system_info.get("arch")
    nodes: dict[int, str] = {int(k): str(v) for k, v in data.get("nodes", {}).items()}
    cpus_dict: dict[int, dict[str, Any]] = {
        int(k): v for k, v in data.get("cpus", {}).items()
    }

    # Normalize socket filter if provided as int/str
    target_socket: int | None = int(socket) if socket is not None else None

    # Step 1: Check sysfs cache data
    has_sysfs_caches = any(
        c.get("caches") for c in cpus_dict.values() if c.get("online", True)
    )

    if has_sysfs_caches:
        # Determine target cache level: prefer L3, fallback to max level found (e.g. L2)
        discovered_levels = set()
        for cpu_info in cpus_dict.values():
            if not cpu_info.get("online", True):
                continue
            for c_entry in cpu_info.get("caches", []):
                lvl = c_entry.get("level")
                if lvl is not None and isinstance(lvl, int):
                    discovered_levels.add(lvl)

        target_level = (
            3
            if 3 in discovered_levels
            else (max(discovered_levels) if discovered_levels else 3)
        )

        # Extract domains from sysfs cache
        # Key: (socket_id, domain_key) where domain_key is frozenset(cpus) or cache_id
        domain_map: dict[tuple[int, Any], dict[str, Any]] = {}

        for cpu_id, cpu_info in sorted(cpus_dict.items()):
            if not cpu_info.get("online", True):
                continue
            pkg_id = cpu_info.get("physical_package_id", 0)
            if not isinstance(pkg_id, int):
                pkg_id = 0
            if target_socket is not None and pkg_id != target_socket:
                continue

            core_id = cpu_info.get("core_id", cpu_id)
            die_id = cpu_info.get("die_id")
            cluster_id = cpu_info.get("cluster_id")

            # Find cache entry for target_level
            cache_entry = None
            for c_entry in cpu_info.get("caches", []):
                if c_entry.get("level") == target_level:
                    # Prefer Unified or Data over Instruction
                    if c_entry.get("type") in ("Unified", "Data", None):
                        cache_entry = c_entry
                        break
                    elif cache_entry is None:
                        cache_entry = c_entry

            if cache_entry:
                shared_list = cache_entry.get("shared_cpu_list")
                shared_map = cache_entry.get("shared_cpu_map")
                cache_id = cache_entry.get("id")
                size = cache_entry.get("size")
                ctype = cache_entry.get("type", "Unified")

                if shared_list:
                    shared_cpus = parse_cpu_list(shared_list)
                elif shared_map:
                    shared_cpus = parse_cpu_mask(shared_map)
                else:
                    shared_cpus = [cpu_id]

                key = (
                    pkg_id,
                    frozenset(shared_cpus) if shared_cpus else cache_id,
                )
                if key not in domain_map:
                    domain_map[key] = {
                        "cache_id": cache_id,
                        "socket_id": pkg_id,
                        "cpus": set(shared_cpus),
                        "cores": {core_id},
                        "size": size,
                        "level": target_level,
                        "type": ctype,
                        "die_id": die_id,
                        "cluster_id": cluster_id,
                        "source": "sysfs",
                    }
                else:
                    domain_map[key]["cpus"].update(shared_cpus)
                    domain_map[key]["cores"].add(core_id)
                    if size and not domain_map[key]["size"]:
                        domain_map[key]["size"] = size
                    if die_id is not None and domain_map[key]["die_id"] is None:
                        domain_map[key]["die_id"] = die_id
                    if cluster_id is not None and domain_map[key]["cluster_id"] is None:
                        domain_map[key]["cluster_id"] = cluster_id
            else:
                # No cache entry for this CPU, fallback to cluster/die or core
                fallback_key = (
                    pkg_id,
                    cluster_id
                    if cluster_id is not None
                    else (die_id if die_id is not None else core_id),
                )
                if fallback_key not in domain_map:
                    domain_map[fallback_key] = {
                        "cache_id": None,
                        "socket_id": pkg_id,
                        "cpus": {cpu_id},
                        "cores": {core_id},
                        "size": None,
                        "level": target_level,
                        "type": "Unified",
                        "die_id": die_id,
                        "cluster_id": cluster_id,
                        "source": "sysfs_cluster_fallback",
                    }
                else:
                    domain_map[fallback_key]["cpus"].add(cpu_id)
                    domain_map[fallback_key]["cores"].add(core_id)

        # Sort domains deterministically: by socket_id, then cache_id, then min cpu
        sorted_domain_items = sorted(
            domain_map.values(),
            key=lambda d: (
                d["socket_id"],
                d["cache_id"] if d["cache_id"] is not None else 999999,
                min(d["cpus"]) if d["cpus"] else 0,
            ),
        )

        domains: list[CacheDomain] = []
        ccds: dict[int, list[int]] = {}
        all_cpus: set[int] = set()

        for idx, d_info in enumerate(sorted_domain_items):
            cpu_list_sorted = sorted(d_info["cpus"])
            all_cpus.update(cpu_list_sorted)
            numa_node = (
                _find_numa_node_for_cpu(cpu_list_sorted[0], nodes)
                if cpu_list_sorted
                else None
            )
            ccd_id = idx
            ccds[ccd_id] = cpu_list_sorted
            domain = CacheDomain(
                ccd_id=ccd_id,
                cache_id=d_info["cache_id"] if d_info["cache_id"] is not None else idx,
                socket_id=d_info["socket_id"],
                numa_node=numa_node,
                cpus=cpu_list_sorted,
                cpu_list=format_cpu_list(cpu_list_sorted),
                core_count=len(d_info["cores"]),
                thread_count=len(cpu_list_sorted),
                size=d_info["size"],
                level=d_info["level"],
                type=d_info["type"],
                die_id=d_info["die_id"],
                cluster_id=d_info["cluster_id"],
                source=d_info["source"],
            )
            domains.append(domain)

        return CacheTopologyResult(
            host=host,
            socket=target_socket,
            ccds=ccds,
            domains=domains,
            total_ccds=len(domains),
            total_cpus=len(all_cpus),
            cache_level=target_level,
            source="sysfs",
            vendor=vendor,
            model=model,
            architecture=architecture,
        )

    # Step 2: Fallback to lscpu output if available
    lscpu_cpus = data.get("lscpu", [])
    if lscpu_cpus:
        return _parse_lscpu_cpus(
            lscpu_cpus,
            host=host,
            socket=target_socket,
            vendor=vendor,
            model=model,
            architecture=architecture,
            nodes=nodes,
        )

    # Step 3: Fallback to /proc/cpuinfo
    cpuinfo_list = data.get("cpuinfo", [])
    if cpuinfo_list:
        return _parse_cpuinfo_records(
            cpuinfo_list,
            host=host,
            socket=target_socket,
            vendor=vendor,
            model=model,
            architecture=architecture,
            nodes=nodes,
        )

    return CacheTopologyResult(
        host=host,
        socket=target_socket,
        ccds={},
        domains=[],
        total_ccds=0,
        total_cpus=0,
        source="none",
        vendor=vendor,
        model=model,
        architecture=architecture,
        error="No CPU cache or topology data could be discovered.",
    )


def _parse_lscpu_cpus(
    lscpu_cpus: list[dict[str, Any]],
    host: str,
    socket: int | None,
    vendor: str | None,
    model: str | None,
    architecture: str | None,
    nodes: dict[int, str],
) -> CacheTopologyResult:
    """Fallback parser for lscpu JSON output."""
    domain_map: dict[tuple[int, Any], dict[str, Any]] = {}

    for entry in lscpu_cpus:
        try:
            cpu_id = int(entry.get("cpu", entry.get("CPU", 0)))
        except (ValueError, TypeError):
            continue

        # Check online
        online_val = entry.get("online", entry.get("ONLINE", True))
        if str(online_val).lower() in ("false", "0", "n", "no"):
            continue

        try:
            socket_id = int(entry.get("socket", entry.get("SOCKET", 0)))
        except (ValueError, TypeError):
            socket_id = 0

        if socket is not None and socket_id != socket:
            continue

        try:
            core_id = int(entry.get("core", entry.get("CORE", cpu_id)))
        except (ValueError, TypeError):
            core_id = cpu_id

        try:
            node_id = int(entry.get("node", entry.get("NODE", -1)))
            if node_id == -1:
                node_id = _find_numa_node_for_cpu(cpu_id, nodes)
        except (ValueError, TypeError):
            node_id = _find_numa_node_for_cpu(cpu_id, nodes)

        # L3 cache mapping: lscpu may give "l3", "L3", or "l1d:l1i:l2:l3"
        l3_val = entry.get("l3", entry.get("L3"))
        if not l3_val and "l1d:l1i:l2:l3" in entry:
            parts = str(entry["l1d:l1i:l2:l3"]).split(":")
            if len(parts) >= 4:
                l3_val = parts[3]

        # Cluster / cache key
        cluster_val = entry.get("cluster", entry.get("CLUSTER"))
        domain_key = (
            l3_val
            if l3_val is not None
            else (cluster_val if cluster_val is not None else core_id)
        )
        key = (socket_id, domain_key)

        if key not in domain_map:
            try:
                cache_id_int = (
                    int(str(l3_val).split(":")[-1])
                    if l3_val is not None and str(l3_val).isdigit()
                    else None
                )
            except Exception:
                cache_id_int = None
            domain_map[key] = {
                "cache_id": cache_id_int,
                "socket_id": socket_id,
                "numa_node": node_id,
                "cpus": {cpu_id},
                "cores": {core_id},
                "size": None,
                "level": 3,
                "type": "Unified",
                "die_id": None,
                "cluster_id": int(cluster_val)
                if cluster_val is not None and str(cluster_val).isdigit()
                else None,
                "source": "lscpu",
            }
        else:
            domain_map[key]["cpus"].add(cpu_id)
            domain_map[key]["cores"].add(core_id)

    sorted_domain_items = sorted(
        domain_map.values(),
        key=lambda d: (
            d["socket_id"],
            d["cache_id"] if d["cache_id"] is not None else 999999,
            min(d["cpus"]) if d["cpus"] else 0,
        ),
    )

    domains: list[CacheDomain] = []
    ccds: dict[int, list[int]] = {}
    all_cpus: set[int] = set()

    for idx, d_info in enumerate(sorted_domain_items):
        cpu_list_sorted = sorted(d_info["cpus"])
        all_cpus.update(cpu_list_sorted)
        ccd_id = idx
        ccds[ccd_id] = cpu_list_sorted
        domain = CacheDomain(
            ccd_id=ccd_id,
            cache_id=d_info["cache_id"] if d_info["cache_id"] is not None else idx,
            socket_id=d_info["socket_id"],
            numa_node=d_info["numa_node"],
            cpus=cpu_list_sorted,
            cpu_list=format_cpu_list(cpu_list_sorted),
            core_count=len(d_info["cores"]),
            thread_count=len(cpu_list_sorted),
            size=d_info["size"],
            level=3,
            type="Unified",
            die_id=d_info["die_id"],
            cluster_id=d_info["cluster_id"],
            source="lscpu",
        )
        domains.append(domain)

    return CacheTopologyResult(
        host=host,
        socket=socket,
        ccds=ccds,
        domains=domains,
        total_ccds=len(domains),
        total_cpus=len(all_cpus),
        cache_level=3,
        source="lscpu",
        vendor=vendor,
        model=model,
        architecture=architecture,
    )


def _parse_cpuinfo_records(
    cpuinfo_list: list[dict[str, str]],
    host: str,
    socket: int | None,
    vendor: str | None,
    model: str | None,
    architecture: str | None,
    nodes: dict[int, str],
) -> CacheTopologyResult:
    """Fallback parser for /proc/cpuinfo."""
    domain_map: dict[tuple[int, Any], dict[str, Any]] = {}

    for rec in cpuinfo_list:
        try:
            cpu_id = int(rec.get("processor", 0))
        except (ValueError, TypeError):
            continue

        try:
            socket_id = int(rec.get("physical id", 0))
        except (ValueError, TypeError):
            socket_id = 0

        if socket is not None and socket_id != socket:
            continue

        try:
            core_id = int(rec.get("core id", cpu_id))
        except (ValueError, TypeError):
            core_id = cpu_id

        try:
            apic_id = int(rec.get("apicid", rec.get("initial apicid", cpu_id)))
        except (ValueError, TypeError):
            apic_id = cpu_id

        # For AMD Zen processors, APIC ID encodes CCD in upper bits:
        is_amd = (vendor == "AuthenticAMD") or ("AMD" in str(model))
        if is_amd and apic_id is not None:
            # Estimate CCD ID from APIC ID core clustering (typical 8 or 16 cores per CCD)
            ccd_key = apic_id >> 4
        else:
            ccd_key = core_id

        key = (socket_id, ccd_key)
        if key not in domain_map:
            domain_map[key] = {
                "cache_id": None,
                "socket_id": socket_id,
                "numa_node": _find_numa_node_for_cpu(cpu_id, nodes),
                "cpus": {cpu_id},
                "cores": {core_id},
                "size": None,
                "level": 3,
                "type": "Unified",
                "die_id": None,
                "cluster_id": None,
                "source": "cpuinfo_apic",
            }
        else:
            domain_map[key]["cpus"].add(cpu_id)
            domain_map[key]["cores"].add(core_id)

    sorted_domain_items = sorted(
        domain_map.values(),
        key=lambda d: (d["socket_id"], min(d["cpus"]) if d["cpus"] else 0),
    )

    domains: list[CacheDomain] = []
    ccds: dict[int, list[int]] = {}
    all_cpus: set[int] = set()

    for idx, d_info in enumerate(sorted_domain_items):
        cpu_list_sorted = sorted(d_info["cpus"])
        all_cpus.update(cpu_list_sorted)
        ccd_id = idx
        ccds[ccd_id] = cpu_list_sorted
        domain = CacheDomain(
            ccd_id=ccd_id,
            cache_id=idx,
            socket_id=d_info["socket_id"],
            numa_node=d_info["numa_node"],
            cpus=cpu_list_sorted,
            cpu_list=format_cpu_list(cpu_list_sorted),
            core_count=len(d_info["cores"]),
            thread_count=len(cpu_list_sorted),
            size=d_info["size"],
            level=3,
            type="Unified",
            die_id=d_info["die_id"],
            cluster_id=d_info["cluster_id"],
            source="cpuinfo_apic",
        )
        domains.append(domain)

    return CacheTopologyResult(
        host=host,
        socket=socket,
        ccds=ccds,
        domains=domains,
        total_ccds=len(domains),
        total_cpus=len(all_cpus),
        cache_level=3,
        source="cpuinfo_apic",
        vendor=vendor,
        model=model,
        architecture=architecture,
    )


async def discover_cache_topology(
    ssh: SSHExecutor,
    host: str,
    socket: int | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Discover CPU cache / CCD topology on a remote host via SSH.

    Args:
        ssh: Configured SSH executor.
        host: Target hostname or IP.
        socket: Optional socket/package ID to filter by.
        timeout: SSH execution timeout in seconds.

    Returns:
        Structured dictionary with CCD mapping and domain details.
    """
    cmd = f"python3 -c {shlex.quote(_TOPOLOGY_COLLECTOR_SCRIPT)}"
    result = await ssh.run(host, cmd, timeout=timeout)

    if result.exit_code != 0:
        # Fallback 1: Try running lscpu -e -J
        lscpu_res = await ssh.run(host, "lscpu -e -J", timeout=timeout)
        if lscpu_res.exit_code == 0:
            try:
                lscpu_data = json.loads(lscpu_res.stdout)
                res = parse_topology_data(
                    {"lscpu": lscpu_data.get("cpus", [])},
                    host=host,
                    socket=socket,
                )
                return res.to_dict()
            except Exception as exc:
                logger.warning("Failed to parse fallback lscpu output: %s", exc)

        return {
            "host": host,
            "socket": socket,
            "error": f"Topology discovery failed (exit {result.exit_code}): {result.stderr or result.stdout}",
            "ccds": {},
            "domains": [],
            "total_ccds": 0,
            "total_cpus": 0,
        }

    try:
        raw_data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "host": host,
            "socket": socket,
            "error": f"Failed to parse topology JSON output: {exc}",
            "ccds": {},
            "domains": [],
            "total_ccds": 0,
            "total_cpus": 0,
        }

    res = parse_topology_data(raw_data, host=host, socket=socket)
    return res.to_dict()
