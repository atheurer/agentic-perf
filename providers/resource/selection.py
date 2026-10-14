"""Provider-specific reservation selection contracts.

The resource tool accepts a generic mapping because it serves several
providers. These models define and validate the mapping after the provider is
known, before the write-ahead reservation marker or any allocation call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)


class ReservationSelectionError(ValueError):
    """A provider selection failed local validation before allocation."""


SelectionValidator = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class _SelectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QuadsSelection(_SelectionModel):
    hostnames: list[StrictStr] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def hostnames_are_distinct(self) -> QuadsSelection:
        if any(not hostname for hostname in self.hostnames):
            raise ValueError("hostnames must be nonempty strings")
        folded = [hostname.casefold() for hostname in self.hostnames]
        if len(set(folded)) != len(folded):
            raise ValueError("hostnames must be distinct")
        return self


class AWSInstanceSpec(_SelectionModel):
    instance_type: StrictStr = Field(min_length=1)
    count: StrictInt = Field(default=1, ge=1)
    role: StrictStr | None = Field(default=None, min_length=1)


class AWSSelection(_SelectionModel):
    instance_specs: list[AWSInstanceSpec] | None = Field(default=None, min_length=1)
    instance_type: StrictStr | None = Field(default=None, min_length=1)
    count: StrictInt | None = Field(default=None, ge=1)
    instance_count: StrictInt | None = Field(default=None, ge=1)
    ami: StrictStr | None = Field(default=None, min_length=1)
    os: StrictStr | None = Field(default=None, min_length=1)
    root_volume_gb: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_one_instance_shape(self) -> AWSSelection:
        has_specs = self.instance_specs is not None
        has_flat_type = self.instance_type is not None
        if has_specs and (
            has_flat_type or self.count is not None or self.instance_count
        ):
            raise ValueError(
                "provide instance_specs or the flat instance_type/count format, "
                "not both"
            )
        if not has_specs and not has_flat_type:
            raise ValueError("instance_specs or instance_type is required")
        if (
            self.count is not None
            and self.instance_count is not None
            and self.count != self.instance_count
        ):
            raise ValueError("count and instance_count must agree when both are set")
        return self


class JumpstarterSelection(_SelectionModel):
    jumpstarter_selector: StrictStr = Field(min_length=1)
    lease_duration_seconds: StrictInt | None = Field(default=None, ge=1)
    count: StrictInt = Field(default=1, ge=1, le=1)
    exporter_name: StrictStr | None = Field(default=None, min_length=1)


class PSAPCCSelection(_SelectionModel):
    cluster_id: StrictStr = Field(min_length=1)


@dataclass(frozen=True)
class ReservationSelectionContract:
    """Validation model and examples used to review each provider contract."""

    model: type[BaseModel]
    valid_example: dict[str, Any]
    invalid_examples: tuple[dict[str, Any], ...]
    prompt_fields: tuple[str, ...]
    validator: SelectionValidator | None = None

    def json_schema(self) -> dict[str, Any]:
        """Return the JSON Schema represented by the validation model."""
        return self.model.model_json_schema()


def _validation_error_message(error: ValidationError) -> str:
    messages = []
    for detail in error.errors(include_url=False):
        location = ".".join(str(part) for part in detail["loc"]) or "selection"
        messages.append(f"{location}: {detail['msg']}")
    return "; ".join(messages)


def _managed_host_count(required_hosts: list[dict[str, Any]] | None) -> int | None:
    if not required_hosts:
        return None
    return sum(
        1 for host in required_hosts if isinstance(host, dict) and not host.get("host")
    )


def _quads_hosts_in_requirement_order(
    selected_hostnames: list[str],
    eligible_hostnames_by_requirement: list[set[str]],
) -> list[str] | None:
    """Match distinct selected hosts and return them in requirement order."""
    if len(eligible_hostnames_by_requirement) == 1 and len(selected_hostnames) > 1:
        eligible_hostnames_by_requirement = eligible_hostnames_by_requirement * len(
            selected_hostnames
        )
    candidates = [
        [
            hostname
            for hostname in selected_hostnames
            if hostname.casefold() in {host.casefold() for host in eligible}
        ]
        for eligible in eligible_hostnames_by_requirement
    ]
    if len(candidates) != len(selected_hostnames):
        return None

    assigned: set[str] = set()
    by_requirement: dict[int, str] = {}

    def assign(requirement_index: int, remaining: list[int]) -> bool:
        if requirement_index == len(remaining):
            return True
        index = remaining[requirement_index]
        for hostname in candidates[index]:
            if hostname in assigned:
                continue
            assigned.add(hostname)
            by_requirement[index] = hostname
            if assign(requirement_index + 1, remaining):
                return True
            by_requirement.pop(index, None)
            assigned.remove(hostname)
        return False

    ordered = sorted(range(len(candidates)), key=lambda index: len(candidates[index]))
    if not assign(0, ordered):
        return None
    return [by_requirement[index] for index in range(len(candidates))]


def _validate_quads_selection(
    normalized: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    hostnames = normalized["hostnames"]
    expected_count = _managed_host_count(context.get("required_hosts"))
    if expected_count is not None and len(hostnames) != expected_count:
        raise ReservationSelectionError(
            f"QUADS selection has {len(hostnames)} hostname(s), but the ticket "
            f"requires {expected_count} managed host(s)"
        )

    eligible = context.get("eligible_hostnames_by_requirement")
    if context.get("require_eligible_hostnames") and eligible is None:
        raise ReservationSelectionError(
            "QUADS eligible host options are unavailable; call "
            "check_available_resources with the ticket's requirements first"
        )
    if eligible is not None:
        ordered = _quads_hosts_in_requirement_order(hostnames, eligible)
        if ordered is None:
            raise ReservationSelectionError(
                "QUADS hostnames must be distinct eligible options matching the "
                "ticket's managed host requirements"
            )
        normalized["hostnames"] = ordered
    return normalized


def _validate_aws_selection(
    normalized: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    expected_count = _managed_host_count(context.get("required_hosts"))
    if expected_count is None:
        return normalized
    specs = normalized.get("instance_specs")
    if specs is not None:
        selected_count = sum(spec.get("count", 1) for spec in specs)
    else:
        selected_count = normalized.get("count", normalized.get("instance_count", 1))
    if selected_count != expected_count:
        raise ReservationSelectionError(
            f"AWS selection covers {selected_count} instance(s), but the ticket "
            f"requires {expected_count} managed host(s)"
        )
    return normalized


def _validate_jumpstarter_selection(
    normalized: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    expected_count = _managed_host_count(context.get("required_hosts"))
    if expected_count is not None and expected_count != 1:
        raise ReservationSelectionError(
            "Jumpstarter leases one device per reservation, but the ticket "
            f"requires {expected_count} managed host(s)"
        )
    return normalized


RESERVATION_SELECTION_CONTRACTS: dict[str, ReservationSelectionContract] = {
    "quads": ReservationSelectionContract(
        model=QuadsSelection,
        valid_example={"hostnames": ["host-01.example.com", "host-02.example.com"]},
        invalid_examples=(
            {"instance_specs": [{"instance_type": "m5.xlarge", "count": 2}]},
            {"hostnames": []},
            {"hostnames": ["host-01.example.com", "HOST-01.EXAMPLE.COM"]},
        ),
        prompt_fields=("hostnames",),
        validator=_validate_quads_selection,
    ),
    "aws": ReservationSelectionContract(
        model=AWSSelection,
        valid_example={
            "instance_specs": [
                {"instance_type": "m5.4xlarge", "count": 1, "role": "controller"},
                {"instance_type": "m5n.4xlarge", "count": 2, "role": "client"},
            ]
        },
        invalid_examples=(
            {"hostnames": ["host-01.example.com"]},
            {"instance_specs": [{"instance_type": "m5.xlarge", "count": 0}]},
            {"instance_specs": [{"instance_type": "m5.xlarge"}], "count": 2},
        ),
        prompt_fields=(
            "instance_specs",
            "instance_type",
            "count",
            "instance_count",
            "ami",
            "os",
            "root_volume_gb",
            "role",
        ),
        validator=_validate_aws_selection,
    ),
    "jumpstarter": ReservationSelectionContract(
        model=JumpstarterSelection,
        valid_example={
            "jumpstarter_selector": "board-type=example",
            "lease_duration_seconds": 14400,
        },
        invalid_examples=(
            {"hostnames": ["board-01"]},
            {"jumpstarter_selector": "board-type=example", "count": 2},
        ),
        prompt_fields=(
            "jumpstarter_selector",
            "lease_duration_seconds",
            "count",
            "exporter_name",
        ),
        validator=_validate_jumpstarter_selection,
    ),
    "psap-cc": ReservationSelectionContract(
        model=PSAPCCSelection,
        valid_example={"cluster_id": "gpu-cluster-01"},
        invalid_examples=({}, {"cluster_id": "gpu-cluster-01", "hostnames": []}),
        prompt_fields=("cluster_id",),
    ),
}


def validate_reservation_selection(
    provider: str,
    selection: Any,
    *,
    required_hosts: list[dict[str, Any]] | None = None,
    eligible_hostnames_by_requirement: list[set[str]] | None = None,
    require_eligible_hostnames: bool = False,
) -> dict[str, Any]:
    """Validate and normalize a selection using its provider's contract."""
    contract = RESERVATION_SELECTION_CONTRACTS.get(provider)
    if contract is None:
        raise ReservationSelectionError(
            f"No reservation selection contract is registered for provider '{provider}'"
        )

    try:
        validated = contract.model.model_validate(selection)
    except ValidationError as exc:
        raise ReservationSelectionError(
            f"Invalid {provider} selection: {_validation_error_message(exc)}"
        ) from None

    normalized = validated.model_dump(exclude_unset=True, exclude_none=True)
    if contract.validator is None:
        return normalized
    return contract.validator(
        normalized,
        {
            "required_hosts": required_hosts,
            "eligible_hostnames_by_requirement": eligible_hostnames_by_requirement,
            "require_eligible_hostnames": require_eligible_hostnames,
        },
    )
