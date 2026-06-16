# ioscale Workloads

ioscale runs storage and database benchmarks inside VMs on
OpenShift Virtualization. It creates VMs with PVC-backed
storage, SSHes into them, installs tools, and runs tests.

## Available Workloads

### ioscale-fio
Storage I/O benchmark using fio inside a VM. Tests read/write
throughput, IOPS, and latency on PVC-backed block storage.

| Parameter | Default | Description |
|-----------|---------|-------------|
| test_size | 1G | FIO test file size |
| runtime | 300 | Seconds per I/O pattern |
| block_sizes | 4k 128k | Space-separated block sizes |
| io_patterns | read write randread randwrite | I/O patterns |
| numjobs | 1 | Parallel FIO jobs |
| iodepth | 16 | I/O queue depth |
| vm_cores | 4 | VM vCPU cores |
| vm_memory | 8Gi | VM memory |
| storage_size | 100Gi | Data disk size |
| storage_class | (auto) | StorageClass name |

### ioscale-mariadb
MariaDB database benchmark using HammerDB TPC-C inside a VM.

| Parameter | Default | Description |
|-----------|---------|-------------|
| warehouse_count | 50 | TPC-C scale factor |
| test_duration | 15 | Minutes per user count |
| user_count | 1 5 10 | Virtual users to test |
| vm_cores | 8 | VM vCPU cores |
| vm_memory | 8Gi | VM memory |
| storage_size | 100Gi | Data disk size |
| storage_class | (auto) | StorageClass name |

### ioscale-postgresql
PostgreSQL database benchmark using HammerDB TPC-C inside
a VM. Same parameters as ioscale-mariadb.

## Storage Requirements

**Critical:** ioscale requires a block-capable StorageClass.
Templates default to `ocs-storagecluster-ceph-rbd` (ODF).
The execution handler overrides this to the actual cluster
StorageClass (e.g., `lvms-vg1` for LVMS).

If `storage_class` is empty, the handler auto-detects the
first available StorageClass on the cluster.

Each VM gets two PVCs:
- Boot disk: 30Gi (Fedora 43 cloud image)
- Data disk: `storage_size` (default 100Gi, blank)

## VM Lifecycle

The execution handler manages the full lifecycle:
1. Creates SSH key secret for VM access
2. Applies VM template with StorageClass override
3. Waits for VM to reach Running state
4. Runs fio-tests.py or mariadb.py/postgresql.py
5. Collects results

## Requirements

- OpenShift 4 with OpenShift Virtualization (CNV)
- Block-capable StorageClass (LVMS, ODF, etc.)
- KUBECONFIG set to valid cluster kubeconfig
- Python 3.9+ with pyyaml and paramiko on controller
