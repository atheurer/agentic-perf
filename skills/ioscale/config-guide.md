# ioscale Config Construction Guide

ioscale uses YAML config files for each test type. The
run-file bundle wraps VM configuration and test parameters.
The execution handler creates VMs, writes configs, and runs
the appropriate Python script.

## Run-File Bundle Format

```json
{
  "harness": "ioscale",
  "test_type": "fio",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "vm_config": {
    "cores": 4,
    "memory": "8Gi",
    "storage_size": "100Gi",
    "storage_class": "lvms-vg1",
    "image_url": "https://dl.fedoraproject.org/...qcow2"
  },
  "test_config": {
    "fio": {
      "test_size": "1G",
      "runtime": 300,
      "block_sizes": "4k 128k",
      "io_patterns": "read write randread randwrite",
      "numjobs": 1,
      "iodepth": 16,
      "direct_io": 1
    }
  }
}
```

## CRITICAL: StorageClass

ioscale templates hardcode `ocs-storagecluster-ceph-rbd`
(OpenShift Data Foundation). The execution handler
automatically overrides this to the actual StorageClass.

Specify `storage_class` explicitly if the cluster has
multiple StorageClasses. If empty, the handler uses the
first available StorageClass.

For LVMS: `"storage_class": "lvms-vg1"`
For ODF: `"storage_class": "ocs-storagecluster-ceph-rbd"`

## vm_config section

| Field | Description |
|-------|-------------|
| cores | vCPU cores for the VM |
| memory | VM memory (e.g., 8Gi) |
| storage_size | Data disk PVC size |
| storage_class | StorageClass override |
| image_url | Fedora cloud image URL |

## test_config.fio section

| Field | Description |
|-------|-------------|
| test_size | FIO file size (e.g., 1G, 10G) |
| runtime | Seconds per I/O pattern |
| block_sizes | Space-separated (e.g., "4k 128k") |
| io_patterns | Space-separated (read write randread randwrite) |
| numjobs | Parallel FIO threads |
| iodepth | I/O queue depth |
| direct_io | 1 for O_DIRECT, 0 for buffered |

## test_config.database section (mariadb/postgresql)

| Field | Description |
|-------|-------------|
| warehouse_count | TPC-C warehouses (scale factor) |
| test_duration | Minutes per user-count run |
| user_count | Space-separated user counts to test |

## Example: FIO with LVMS

```json
{
  "harness": "ioscale",
  "test_type": "fio",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "vm_config": {
    "cores": 4,
    "memory": "4Gi",
    "storage_size": "100Gi",
    "storage_class": "lvms-vg1"
  },
  "test_config": {
    "fio": {
      "test_size": "1G",
      "runtime": 120,
      "block_sizes": "4k",
      "io_patterns": "randread",
      "numjobs": 1,
      "iodepth": 16,
      "direct_io": 1
    }
  }
}
```

## Example: MariaDB HammerDB

```json
{
  "harness": "ioscale",
  "test_type": "mariadb",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "vm_config": {
    "cores": 8,
    "memory": "8Gi",
    "storage_size": "100Gi",
    "storage_class": "lvms-vg1"
  },
  "test_config": {
    "database": {
      "warehouse_count": 50,
      "test_duration": 15,
      "user_count": "1 5 10"
    }
  }
}
```

## Installation

```bash
git clone https://github.com/ekuric/ioscale.git /opt/ioscale
pip install pyyaml paramiko
```

## Execution Flow

The handler:
1. Auto-detects StorageClass if not specified
2. Creates SSH key secret (`vmkeyroot`)
3. Patches VM template (StorageClass, cores, memory)
4. Applies VM via `oc apply`
5. Waits for VM Running + gets IP
6. Writes test YAML config
7. Runs: `python3 fio-tests.py -c <config>` or
   `python3 mariadb.py -c <config>`
8. Returns results + VM name for cleanup

## Prerequisites

- OpenShift Virtualization (CNV) operator
- Block-capable StorageClass (LVMS, ODF, NFS-block)
- Python 3.9+ with pyyaml, paramiko on controller
- SSH key secret for VM access (created by handler)
