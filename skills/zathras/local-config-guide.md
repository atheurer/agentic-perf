# Zathras Local Config for Storage and Network Benchmarks

## When You Need This

Storage benchmarks (fio, iozone, hammerdb, speccpu2017) and network benchmarks
(uperf) require a `local_configs/<hostname>.config` file on the controller.
Without it, burden's preflight check fails with:

```
Error: local_configs/<hostname>.config does not exist.
Required for local systems when storage is needed.
```

CPU-only benchmarks (streams, coremark, coremark_pro, linpack, pig, pyperf,
auto_hpl, phoronix_*) do NOT need a local config file.

## How the execute_benchmark Tool Handles This

The `execute_benchmark` tool creates the local config file automatically when
you include `local_config` and `host_config_name` in the run_file. You do NOT
need to SSH to the controller to create the file manually.

### Run File Structure for Storage Benchmarks

```json
{
  "scenario": {
    "global": {
      "results_prefix": "fio_test",
      "system_type": "local",
      "os_vendor": "rhel"
    },
    "systems": {
      "system1": {
        "tests": "fio",
        "host_config": "nfv-amd-5.perf.eng.bos2.dc.redhat.com"
      }
    }
  },
  "local_config": {
    "storage": "/dev/nvme0n1"
  },
  "host_config_name": "nfv-amd-5.perf.eng.bos2.dc.redhat.com"
}
```

The tool writes `local_configs/nfv-amd-5.perf.eng.bos2.dc.redhat.com.config`
on the controller with the contents of `local_config` before running burden.

### Run File Structure for Network Benchmarks (uperf)

```json
{
  "scenario": {
    "global": {
      "results_prefix": "uperf_test",
      "system_type": "local",
      "os_vendor": "rhel"
    },
    "systems": {
      "system1": {
        "tests": "uperf",
        "host_config": "client-host.example.com"
      }
    }
  },
  "local_config": {
    "server_ips": "192.168.1.100",
    "client_ips": "192.168.1.101"
  },
  "host_config_name": "client-host.example.com"
}
```

## Critical Rules

1. **host_config_name MUST match host_config**: The `host_config_name` value
   must exactly match the `host_config` value in the scenario's system section.
   Use the full FQDN, not a short hostname.

2. **host_config_name MUST be the FQDN**: Use `nfv-amd-5.perf.eng.bos2.dc.redhat.com`,
   not `nfv-amd-5`. Burden looks for `local_configs/<host_config>.config` and the
   host_config in the scenario must match.

3. **Storage field format**: Comma-separated block device paths.
   Example: `"/dev/nvme0n1,/dev/nvme1n1"`

4. **Network field format**: Comma-separated IPs or hostnames.
   `server_ips` is the uperf server, `client_ips` is the uperf client.

## Storage Device Discovery

If the user says "discover available storage" and you don't know which devices
are available, check the ticket's parsed_specs or resource agent comments for
hardware details. The resource agent's validate_host output may include disk
information. If no storage info is available in the ticket, ask the user via
request_clarification.
