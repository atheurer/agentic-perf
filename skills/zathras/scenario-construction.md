# Zathras Scenario Construction Guide

## Run File Structure

The execute_benchmark tool expects this run_file structure for zathras:

```json
{
  "scenario": {
    "global": { ... },
    "systems": { ... }
  },
  "local_config": { ... },
  "host_config_name": "hostname"
}
```

The tool extracts `scenario` and writes it as YAML. If `local_config` and
`host_config_name` are present, it writes a local config file on the controller
before running burden.

## Valid Scenario Keys

Scenario keys map 1:1 to burden CLI flags (minus the dashes). Only keys that
correspond to a `--flag` are valid.

### Common Global Keys

- `results_prefix` — label for the results directory
- `system_type` — `local` for bare metal
- `ssh_key_file` — absolute path to SSH private key (tilde NOT expanded)
- `test_user` — SSH user (default: root)
- `os_vendor` — `rhel`, `ubuntu`, `amazon`, `suse`
- `test_iter` — number of test iterations (default: 1)
- `no_clean_up` — do not clean up after test (flag, no value)
- `use_pcp` — `0` or `1`, toggle Performance Co-Pilot

### Common System Keys

- `tests` — test name(s), comma-separated
- `host_config` — hostname (must match local_configs filename for storage/network tests)
- `java_version` — `java-8`, `java-11`, `java-17`, `java-21`
- `tuned_profiles` — comma-separated tuned profiles (RHEL only)

## Things That Are NOT Scenario Keys

- `test_specific` — this is a test definition field in `config/test_defs.yml`,
  not a scenario field. To override test params, use `test_override` in global:
  `"test_override": "system1:test_specific=--iterations 3"`

- `pre_run_script`, `post_run_script` — not recognized by burden

## Boolean Flags

Options like `no_clean_up`, `no_packages`, `skip_test_version_check` are flags
that take NO argument value. Do not use `true` or `false`. Use an empty string:

```json
"no_clean_up": ""
```

The execute_benchmark tool handles this automatically for common flags.

## SSH Key Path

Use absolute paths. Zathras does not expand `~`. The execute_benchmark tool
auto-expands tilde to `/root` for the `ssh_key_file` field.
