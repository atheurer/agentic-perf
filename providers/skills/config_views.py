"""Typed, allowlisted model-facing views over service-only config."""

from __future__ import annotations

import re
from typing import Any

from .gateway import SkillGatewayError

_VIEWS_BY_PHASE = {
    "triage": (),
    "provisioning": ("constraints", "provisioning", "platform_contract"),
    "benchmark": ("execution", "firewall"),
    "review": ("review",),
}

_FIELDS_BY_VIEW = {
    "constraints": {
        "supported_os": "string_list",
        "controller_os_must_match": "bool",
    },
    "provisioning": {
        "method": "identifier",
        "install_method": "identifier",
        "on_existing_install": "existing_action",
        "install_target_path": "path",
        "install_dir": "path",
    },
    "platform_contract": {
        "supported_os": "string_list",
        "required_packages": "package_list_or_map",
    },
    "execution": {
        "controller_required": "bool",
        "endpoint_type": "endpoint_type",
        "endpoint_user": "identifier",
        "default_osruntime": "identifier",
        "default_userenv": "image_ref",
        "run_file_format": "identifier",
        "run_file_location": "path",
        "results_dir_pattern": "path",
    },
    "review": {
        "method": "identifier",
        "results_method": "identifier",
        "cdm_port": "port",
        "result_summary_path": "path",
        "result_summary_file": "path",
        "results_dir_pattern": "path",
    },
    "firewall": {
        "disable": "bool",
        "disable_firewall": "bool",
        "policy": "identifier",
    },
}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,255}$")
_PATH = re.compile(r"^[A-Za-z0-9_./{}*:%-]{0,512}$")
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:^|[._/:-])(?:password|secret|token|credential|auth)(?:$|[._/:-])"
)
_EXISTING_ACTIONS = frozenset({"skip", "update", "reinstall", "ask_user"})


def config_views_for(harness: str, phase: str) -> tuple[str, ...]:
    """List the explicitly registered model-facing views for this phase."""
    if harness != "crucible":
        return ()
    return _VIEWS_BY_PHASE.get(phase, ())


def _invalid_field(view: str, field: str) -> SkillGatewayError:
    return SkillGatewayError(
        "configuration_view_invalid",
        f"Configured {view}.{field} cannot be safely projected",
    )


def _safe_identifier(value: Any, view: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or _SENSITIVE_VALUE.search(value)
    ):
        raise _invalid_field(view, field)
    return value


def _safe_path(value: Any, view: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not _PATH.fullmatch(value)
        or ".." in value.split("/")
        or "://" in value
        or _SENSITIVE_VALUE.search(value)
    ):
        raise _invalid_field(view, field)
    return value


def _safe_image_ref(value: Any, view: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not _IMAGE_REF.fullmatch(value)
        or "@" in value.split("/", 1)[0]
        or "://" in value
        or _SENSITIVE_VALUE.search(value)
    ):
        raise _invalid_field(view, field)
    return value


def _string_list(value: Any, view: str, field: str) -> list[str]:
    if not isinstance(value, list):
        raise _invalid_field(view, field)
    normalized = []
    for item in value:
        normalized.append(_safe_identifier(item, view, field))
    return list(dict.fromkeys(normalized))


def _package_list_or_map(
    value: Any, view: str, field: str
) -> list[str] | dict[str, list[str]]:
    if isinstance(value, list):
        return _string_list(value, view, field)
    if isinstance(value, dict):
        return {
            _safe_identifier(os_name, view, field): _string_list(packages, view, field)
            for os_name, packages in sorted(value.items())
        }
    raise _invalid_field(view, field)


def _normalize_field(value: Any, kind: str, view: str, field: str) -> Any:
    if kind == "bool":
        if type(value) is not bool:
            raise _invalid_field(view, field)
        return value
    if kind == "port":
        if type(value) is not int or not 1 <= value <= 65535:
            raise _invalid_field(view, field)
        return value
    if kind == "identifier":
        return _safe_identifier(value, view, field)
    if kind == "image_ref":
        return _safe_image_ref(value, view, field)
    if kind == "endpoint_type":
        if not isinstance(value, str) or value not in {"remotehosts", "kube"}:
            raise _invalid_field(view, field)
        return value
    if kind == "path":
        return _safe_path(value, view, field)
    if kind == "string_list":
        return _string_list(value, view, field)
    if kind == "package_list_or_map":
        return _package_list_or_map(value, view, field)
    if kind == "existing_action":
        if not isinstance(value, str) or value not in _EXISTING_ACTIONS:
            raise _invalid_field(view, field)
        return value
    raise _invalid_field(view, field)


def project_config_view(
    harness: str,
    view: str,
    phase: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Validate and project one registered view, excluding commands/secrets."""
    if view not in config_views_for(harness, phase):
        raise SkillGatewayError(
            "configuration_view_denied", "No approved view for this subject and phase"
        )
    if not isinstance(config, dict):
        raise SkillGatewayError(
            "configuration_view_invalid", "Configured runtime settings are invalid"
        )

    section = config.get(view)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SkillGatewayError(
            "configuration_view_invalid", f"Configured {view} must be an object"
        )

    result = {
        field: _normalize_field(section[field], kind, view, field)
        for field, kind in _FIELDS_BY_VIEW[view].items()
        if field in section
    }
    if view == "provisioning" and "options_on_existing" in section:
        options = section["options_on_existing"]
        if isinstance(options, dict):
            # Extension maps have no registered public shape; keep them hidden.
            pass
        elif not isinstance(options, list):
            raise _invalid_field(view, "options_on_existing")
        else:
            result["options_on_existing"] = []
            for item in options:
                if not isinstance(item, dict) or "action" not in item:
                    raise _invalid_field(view, "options_on_existing")
                result["options_on_existing"].append(
                    {
                        "action": _normalize_field(
                            item["action"],
                            "existing_action",
                            view,
                            "options_on_existing",
                        )
                    }
                )
    if view == "execution" and "kube" in section:
        kube = section["kube"]
        if not isinstance(kube, dict):
            raise _invalid_field(view, "kube")
        kube_fields = {
            "min_root_volume_gb": "positive_int",
            "self_ssh_required": "bool",
            "selinux": "identifier",
            "tool_params_required": "bool",
        }
        projected_kube = {}
        for field, kind in kube_fields.items():
            if field not in kube:
                continue
            if kind == "positive_int":
                value = kube[field]
                if type(value) is not int or value < 0:
                    raise _invalid_field("execution", f"kube.{field}")
                projected_kube[field] = value
            else:
                projected_kube[field] = _normalize_field(
                    kube[field], kind, "execution", f"kube.{field}"
                )
        if projected_kube:
            result["kube"] = projected_kube
    return result
