"""Structural validation for service-only subject configuration.

Schemas describe operation contracts, without prescribing organization values.
Unknown keys remain available to provider extensions. Configuration validation
does not authorize operations or turn secret references into secret values.
"""

from __future__ import annotations

from typing import Any


def _fields(section: dict[str, Any], types: dict[str, tuple[type, ...]]) -> None:
    for key, accepted in types.items():
        if key in section and type(section[key]) not in accepted:
            raise ValueError(f"Invalid runtime configuration type for {key}")


def validate_runtime_config(subject: str, config: dict[str, Any]) -> None:
    """Validate registered operation sections; leave other subjects extensible."""
    if not isinstance(config, dict):
        raise ValueError("Runtime configuration must be an object")
    if subject != "harness/crucible":
        return
    for name in (
        "constraints",
        "execution",
        "provisioning",
        "review",
        "secrets",
        "install_contract",
        "platform_contract",
        "firewall",
    ):
        if name in config and not isinstance(config[name], dict):
            raise ValueError(f"Runtime configuration section {name} must be an object")
    _fields(
        config.get("execution", {}),
        {
            "controller_required": (bool,),
            "run_command": (str,),
            "run_file_location": (str,),
            "run_file_format": (str,),
            "results_dir_pattern": (str,),
            "userenv_discovery": (str, dict),
            "default_userenv": (str,),
            "default_osruntime": (str,),
            "endpoint_type": (str,),
            "endpoint_user": (str,),
            "pre_run": (list, dict),
            "kube": (dict,),
        },
    )
    _fields(
        config.get("provisioning", {}),
        {
            "install_method": (str,),
            "installer_url": (str,),
            "install_command": (str,),
            "install_flags": (str, list, dict),
            "verify_command": (str,),
            "update_command": (str,),
            "on_existing_install": (str,),
            "options_on_existing": (list, dict),
            "install_target_path": (str,),
            "install_target_dir": (str,),
            "pre_uninstall_commands": (list,),
            "pre_install_commands": (list,),
            "post_install_commands": (list,),
        },
    )
    _fields(
        config.get("review", {}),
        {
            "results_method": (str,),
            "cdm_port": (int,),
            "result_summary_path": (str,),
        },
    )
    port = config.get("review", {}).get("cdm_port")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Runtime configuration CDM port is outside valid range")
    _fields(
        config.get("constraints", {}),
        {"supported_os": (list,), "controller_os_must_match": (bool,)},
    )
    _fields(config.get("install_contract", {}), {"secret_files": (list,)})
    _fields(
        config.get("platform_contract", {}),
        {"supported_os": (list,), "required_packages": (list, dict)},
    )
    provisioning = config.get("provisioning", {})
    for key in (
        "pre_uninstall_commands",
        "pre_install_commands",
        "post_install_commands",
    ):
        if any(not isinstance(item, str) for item in provisioning.get(key, [])):
            raise ValueError("Runtime command lists must contain strings")
    flags = provisioning.get("install_flags")
    if isinstance(flags, dict) and any(
        not isinstance(key, str) or (value is not None and not isinstance(value, str))
        for key, value in flags.items()
    ):
        raise ValueError("Runtime installation flags must map names to strings or null")
    execution = config.get("execution", {})
    pre_run = execution.get("pre_run", [])
    if isinstance(pre_run, list):
        for step in pre_run:
            if not isinstance(step, (str, dict)):
                raise ValueError("Runtime pre-run steps must be strings or objects")
            if isinstance(step, dict):
                _fields(step, {"name": (str,), "method": (str,)})
    discovery = execution.get("userenv_discovery")
    if isinstance(discovery, dict):
        _fields(
            discovery,
            {"required": (bool,), "command": (str,), "source": (str,)},
        )
    _fields(
        execution.get("kube", {}),
        {
            "min_root_volume_gb": (int,),
            "self_ssh_required": (bool,),
            "selinux": (str,),
            "tool_params_required": (bool,),
        },
    )
    install = config.get("install_contract", {})
    _fields(install, {"pre_install_commands": (list,)})
    if any(
        not isinstance(item, str) for item in install.get("pre_install_commands", [])
    ):
        raise ValueError("Runtime installation command lists must contain strings")
    if any(not isinstance(value, str) for value in config.get("secrets", {}).values()):
        raise ValueError("Runtime secret references must be strings")
    for item in install.get("secret_files", []):
        if not isinstance(item, dict):
            raise ValueError("Runtime secret file entries must be objects")
        _fields(
            item,
            {"secret_key": (str,), "remote_path": (str,), "required": (bool,)},
        )
