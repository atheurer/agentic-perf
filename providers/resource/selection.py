"""Provider-specific reservation selection contracts.

The resource tool accepts a generic mapping because it serves several
providers. These models define and validate the mapping after the provider is
known, before the write-ahead reservation marker or any allocation call.
"""

from __future__ import annotations

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

    def json_schema(self) -> dict[str, Any]:
        """Return the JSON Schema represented by the validation model."""
        return self.model.model_json_schema()


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
    ),
    "psap-cc": ReservationSelectionContract(
        model=PSAPCCSelection,
        valid_example={"cluster_id": "gpu-cluster-01"},
        invalid_examples=({}, {"cluster_id": "gpu-cluster-01", "hostnames": []}),
        prompt_fields=("cluster_id",),
    ),
}


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


def _quads_hosts_match_requirements(
    selected_hostnames: list[str],
    eligible_hostnames_by_requirement: list[set[str]],
) -> bool:
    """Check that each selected host satisfies a distinct host requirement."""
    selected = {hostname.casefold() for hostname in selected_hostnames}
    if len(eligible_hostnames_by_requirement) == 1 and len(selected) > 1:
        eligible_hostnames_by_requirement = eligible_hostnames_by_requirement * len(
            selected
        )
    candidates = [
        sorted(selected.intersection(host.casefold() for host in eligible))
        for eligible in eligible_hostnames_by_requirement
    ]
    if len(candidates) != len(selected):
        return False

    assigned: set[str] = set()

    def assign(requirement_index: int, remaining: list[int]) -> bool:
        if requirement_index == len(remaining):
            return True
        index = remaining[requirement_index]
        for hostname in candidates[index]:
            if hostname in assigned:
                continue
            assigned.add(hostname)
            if assign(requirement_index + 1, remaining):
                return True
            assigned.remove(hostname)
        return False

    ordered = sorted(range(len(candidates)), key=lambda index: len(candidates[index]))
    return assign(0, ordered)


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
    expected_count = _managed_host_count(required_hosts)

    if provider == "quads":
        hostnames = normalized["hostnames"]
        if expected_count is not None and len(hostnames) != expected_count:
            raise ReservationSelectionError(
                f"QUADS selection has {len(hostnames)} hostname(s), but the ticket "
                f"requires {expected_count} managed host(s)"
            )
        if require_eligible_hostnames and eligible_hostnames_by_requirement is None:
            raise ReservationSelectionError(
                "QUADS eligible host options are unavailable; call "
                "check_available_resources with the ticket's requirements first"
            )
        if eligible_hostnames_by_requirement is not None and not (
            _quads_hosts_match_requirements(
                hostnames, eligible_hostnames_by_requirement
            )
        ):
            raise ReservationSelectionError(
                "QUADS hostnames must be distinct eligible options matching the "
                "ticket's managed host requirements"
            )

    elif provider == "aws" and expected_count is not None:
        specs = normalized.get("instance_specs")
        if specs is not None:
            selected_count = sum(spec.get("count", 1) for spec in specs)
        else:
            selected_count = normalized.get(
                "count", normalized.get("instance_count", 1)
            )
        if selected_count != expected_count:
            raise ReservationSelectionError(
                f"AWS selection covers {selected_count} instance(s), but the ticket "
                f"requires {expected_count} managed host(s)"
            )

    elif provider == "jumpstarter" and expected_count is not None:
        if expected_count != 1:
            raise ReservationSelectionError(
                "Jumpstarter leases one device per reservation, but the ticket "
                f"requires {expected_count} managed host(s)"
            )

    return normalized
