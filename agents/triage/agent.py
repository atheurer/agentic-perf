from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition

from .prompts import TRIAGE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def _known_harness_names(skill_provider: Any) -> set[str]:
    """Return catalog harness names, standalone harnesses, and aliases."""
    from providers.skills.base import HARNESS_ALIASES
    from providers.skills.catalog import STANDALONE_BENCHMARKS

    names = (
        set(skill_provider.list_harnesses())
        if hasattr(skill_provider, "list_harnesses")
        else set()
    )
    names.update(suite.harness for suite in STANDALONE_BENCHMARKS)
    names.update(HARNESS_ALIASES)
    names.update(HARNESS_ALIASES.values())
    return names


@dataclass(frozen=True)
class _HarnessIntent:
    required: frozenset[str]
    alternatives: tuple[frozenset[str], ...]
    excluded: frozenset[str]


def _description_harness_intent(
    description: str,
    harness_names: set[str],
) -> _HarnessIntent:
    """Extract required, alternative, and explicitly excluded harnesses."""
    if not description:
        return _HarnessIntent(frozenset(), (), frozenset())

    from providers.skills.base import HARNESS_ALIASES

    aliases = {alias.casefold(): target for alias, target in HARNESS_ALIASES.items()}
    occurrences: list[dict[str, Any]] = []
    for candidate in sorted(harness_names, key=len, reverse=True):
        token = rf"(?<![\w-]){re.escape(candidate)}(?![\w-])"
        canonical = aliases.get(candidate.casefold(), candidate)
        for match in re.finditer(token, description, re.IGNORECASE):
            clause_start = (
                max(
                    description.rfind(separator, 0, match.start())
                    for separator in ".!?;\n"
                )
                + 1
            )
            occurrences.append(
                {
                    "start": match.start(),
                    "end": match.end(),
                    "clause_start": clause_start,
                    "canonical": canonical,
                }
            )

    occurrences.sort(key=lambda item: (item["start"], -item["end"]))
    # A canonical name and alias may overlap in a name set. Keep only the
    # longest match at a given location so it cannot create duplicate intent.
    unique_occurrences: list[dict[str, Any]] = []
    for occurrence in occurrences:
        if (
            unique_occurrences
            and occurrence["start"] == unique_occurrences[-1]["start"]
        ):
            continue
        unique_occurrences.append(occurrence)
    occurrences = unique_occurrences

    negative_patterns = (
        r"\b(?:do\s+not|don't|dont|never|should\s+not|shouldn't)\s+"
        r"(?:(?:use|using|with|via|choose|select|prefer)\s+)?"
        r"(?:(?:the|either)\s+)?(?:harness\s+)?$",
        r"\bavoid\s+(?:(?:use|using)\s+)?"
        r"(?:(?:the|either)\s+)?(?:harness\s+)?$",
        r"\bnot\s+(?:the\s+)?(?:harness\s+)?$",
        r"\b(?:rather\s+than|instead\s+of)\s+(?:the\s+)?(?:harness\s+)?$",
    )
    positive_patterns = (
        r"\b(?:use|using|with|via|choose|select|prefer)\s+"
        r"(?:(?:the|either|only)\s+)?(?:harness\s+)?$",
        r"\bharness\b\s*(?:(?:is|should\s+be|to)\s+|[:=]\s*)$",
        r"\bbut\s+(?:(?:use|using|choose|select|prefer)\s+)?$",
    )

    def connector_between(left: dict[str, Any], right: dict[str, Any]) -> str | None:
        between = description[left["end"] : right["start"]]
        if re.fullmatch(r"[\s,]*or\s+(?:(?:the)\s+)?", between, re.I):
            return "or"
        if re.fullmatch(r"[\s,]*and\s+(?:(?:the)\s+)?", between, re.I):
            return "and"
        if re.fullmatch(r"\s*,\s*", between):
            return "comma"
        return None

    for index, occurrence in enumerate(occurrences):
        prefix = description[
            max(occurrence["clause_start"], occurrence["start"] - 120) : occurrence[
                "start"
            ]
        ]
        if any(
            re.search(pattern, prefix, re.IGNORECASE) for pattern in negative_patterns
        ):
            occurrence["polarity"] = "negative"
            continue
        if any(
            re.search(pattern, prefix, re.IGNORECASE) for pattern in positive_patterns
        ):
            occurrence["polarity"] = "positive"
            continue

        # A name connected by "or" or "and" inherits the selection's
        # polarity. This handles both "Use X or Y" and "Do not use X or Y".
        previous = occurrences[index - 1] if index else None
        if previous and previous["clause_start"] == occurrence["clause_start"]:
            connector = connector_between(previous, occurrence)
            if connector in {"or", "and"}:
                occurrence["polarity"] = previous.get("polarity")
                continue
            if connector == "comma" and previous.get("polarity"):
                # A comma-separated name inherits the list's polarity when a
                # later item makes the list coordination explicit.
                next_index = index + 1
                while next_index < len(occurrences):
                    following = occurrences[next_index]
                    if following["clause_start"] != occurrence["clause_start"]:
                        break
                    later_connector = connector_between(
                        occurrences[next_index - 1], following
                    )
                    if later_connector in {"or", "and"}:
                        occurrence["polarity"] = previous["polarity"]
                        break
                    if later_connector != "comma":
                        break
                    next_index += 1
                if occurrence.get("polarity"):
                    continue
        occurrence["polarity"] = None

    excluded = {
        occurrence["canonical"]
        for occurrence in occurrences
        if occurrence.get("polarity") == "negative"
    }
    required: set[str] = set()
    alternatives: list[frozenset[str]] = []
    index = 0
    while index < len(occurrences):
        occurrence = occurrences[index]
        if occurrence.get("polarity") != "positive":
            index += 1
            continue

        group = {occurrence["canonical"]}
        next_index = index
        has_or = False
        while next_index + 1 < len(occurrences):
            following = occurrences[next_index + 1]
            if (
                following.get("polarity") != "positive"
                or following["clause_start"] != occurrence["clause_start"]
            ):
                break
            connector = connector_between(occurrences[next_index], following)
            if connector not in {"comma", "or"}:
                break
            has_or |= connector == "or"
            group.add(following["canonical"])
            next_index += 1
        if next_index > index and has_or:
            alternatives.append(frozenset(group))
            index = next_index + 1
        else:
            required.add(occurrence["canonical"])
            index += 1

    return _HarnessIntent(frozenset(required), tuple(alternatives), frozenset(excluded))


def _description_requested_harnesses(
    description: str,
    harness_names: set[str],
) -> set[str]:
    """Return all positively named selections, including alternatives."""
    intent = _description_harness_intent(description, harness_names)
    requested = set(intent.required)
    for alternatives in intent.alternatives:
        requested.update(alternatives)
    return requested


def _harness_intent_conflict(intent: _HarnessIntent, harness: str) -> str | None:
    """Describe why a candidate harness violates explicit user intent."""
    if harness and harness in intent.excluded:
        return f"The request explicitly excludes harness '{harness}'."
    if len(intent.required) > 1:
        names = ", ".join(sorted(intent.required))
        return f"The request names conflicting harnesses ({names})."
    if intent.required:
        required = next(iter(intent.required))
        if required in intent.excluded:
            return f"The request both selects and excludes harness '{required}'."
        if harness != required:
            return (
                f"The request specifies harness '{required}', but triage selected "
                f"'{harness or 'no harness'}'."
            )
    for choices in intent.alternatives:
        permitted = choices - intent.excluded
        if not permitted:
            names = ", ".join(sorted(choices))
            return f"The request excludes every listed harness alternative ({names})."
        if harness not in permitted:
            names = ", ".join(sorted(permitted))
            return (
                f"The request permits harness alternatives ({names}), but triage "
                f"selected '{harness or 'no harness'}'."
            )
    return None


def _description_requests_harness(description: str, harness: str) -> bool:
    """Return whether the description explicitly selects this harness."""
    from providers.skills.base import HARNESS_ALIASES

    canonical = HARNESS_ALIASES.get(harness, harness)
    candidates = {harness, canonical}
    candidates.update(
        alias for alias, target in HARNESS_ALIASES.items() if target == canonical
    )
    return canonical in _description_requested_harnesses(description, candidates)


# _canonicalize_workflow_harness removed — workflow_source → harness
# mapping is now handled by AgentBase._effective_harness, which is
# the single source of truth for harness resolution.  The triage
# agent writes the canonical name back to directives after calling
# _effective_harness.


async def _validate_harness_and_suite(
    harness: str,
    benchmark_suite: str,
    absent_suite: bool,
    skill_provider: Any,
) -> dict[str, Any] | None:
    """Validate that harness and benchmark suite exist in the catalog.

    Returns None when valid.  Returns a dict with 'error', 'available_harnesses',
    and optionally 'correction' when the harness or suite is invalid.
    """
    known_harnesses = _known_harness_names(skill_provider)

    available_list = sorted(known_harnesses)

    # 1. Validate harness name exists.
    if harness and harness not in known_harnesses:
        return {
            "error": (
                f"Harness '{harness}' is not a recognized harness name. "
                f"Available harnesses: {available_list}"
            ),
            "available_harnesses": available_list,
        }

    # 2. Validate every nonempty benchmark suite. The LLM's absent_suite
    #    flag is advisory; an invented suite must not reach dispatch.
    if benchmark_suite:
        # Try to find the suite in the catalog — maybe the LLM
        # built a wrong composite name (e.g. "jumpstarter-boot-time"
        # instead of "boot-time").
        from providers.skills.catalog import get_catalog_benchmark

        found = await get_catalog_benchmark(skill_provider, benchmark_suite)
        if found is not None:
            correction: dict[str, Any] = {}
            if absent_suite:
                # The suite exists; the LLM was wrong about absent_suite.
                correction.update(
                    absent_suite=False,
                    benchmark_suite=benchmark_suite,
                )
            catalog_harness = found.get("harness")
            if catalog_harness and catalog_harness != harness:
                correction.update(
                    benchmark_suite=benchmark_suite,
                    harness=catalog_harness,
                    note=(
                        f"Using catalog harness '{catalog_harness}' for "
                        f"benchmark suite '{benchmark_suite}'"
                    ),
                )
            return {"correction": correction} if correction else None
        # Strip only a recognized harness or resource-provider prefix.
        # This accepts names such as "kube-burner-uperf" and
        # "jumpstarter-boot-time" without treating arbitrary prefixes
        # like "custom-uperf" as a valid correction.
        from providers.resource.registry import PROVIDER_REGISTRY

        recognized_prefixes = known_harnesses | set(PROVIDER_REGISTRY)
        for prefix in sorted(recognized_prefixes, key=len, reverse=True):
            prefix_marker = f"{prefix}-"
            if not benchmark_suite.startswith(prefix_marker):
                continue
            suffix = benchmark_suite[len(prefix_marker) :]
            if not suffix:
                continue
            found = await get_catalog_benchmark(skill_provider, suffix)
            if found is not None:
                return {
                    "correction": {
                        "absent_suite": False,
                        "benchmark_suite": suffix,
                        "harness": found.get("harness", harness),
                        "note": (
                            f"Auto-corrected benchmark suite from "
                            f"'{benchmark_suite}' to '{suffix}'"
                        ),
                    },
                }
        if not absent_suite:
            # Mark an unrecognized suite absent so the orchestrator's
            # existing human-guidance path blocks execution.
            return {"correction": {"absent_suite": True}}

    return None


def _filter_direct_required_hosts(
    required_hosts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove controller roles while retaining other host requirements."""
    filtered: list[dict[str, Any]] = []
    for host in required_hosts:
        roles = host.get("roles", [])
        remaining_roles = [role for role in roles if role != "controller"]
        if remaining_roles:
            filtered_host = dict(host)
            filtered_host["roles"] = remaining_roles
            filtered.append(filtered_host)
    return filtered or [{"roles": ["client"]}]


_SCOPED_CONTEXT_CALL_RE = re.compile(
    r'_get_scoped_context\(\s*ticket\s*,\s*["\'](\w+)["\']'
)


def _discover_scoped_context_keys() -> frozenset[str]:
    """Scan agent modules for _get_scoped_context calls to find valid keys.

    Runs once at import time so the set stays in sync automatically when
    new agents are added without requiring a manual update here.
    """
    keys: set[str] = {"shared"}
    agents_dir = Path(__file__).parent.parent
    for agent_file in sorted(agents_dir.glob("*/agent.py")):
        for match in _SCOPED_CONTEXT_CALL_RE.finditer(agent_file.read_text()):
            keys.add(match.group(1))
    return frozenset(keys)


_KNOWN_SCOPED_CONTEXT_KEYS = _discover_scoped_context_keys()

_LOCAL_TOOLS = [
    ToolDefinition(
        name="request_clarification",
        description=(
            "Ask the user for clarification when the test request is "
            "ambiguous or missing critical information. This will pause "
            "the ticket and wait for human input."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to ask the user",
                }
            },
            "required": ["question"],
        },
    ),
    ToolDefinition(
        name="submit_triage_result",
        description=(
            "Submit the triage result when analysis is complete. "
            "Call this tool with your findings."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "parsed_specs": {
                    "type": "object",
                    "description": (
                        "Hardware/software specs extracted from the request"
                    ),
                },
                "hypothesis": {
                    "type": "string",
                    "description": "What the user wants to prove or disprove",
                },
                "benchmark_suite": {
                    "type": "string",
                    "description": "The resolved benchmark suite name",
                },
                "absent_suite": {
                    "type": "boolean",
                    "description": (
                        "True if no automation suite covers this benchmark"
                    ),
                },
                "required_hosts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "roles": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Roles this host serves (e.g. "
                                    '["controller"], ["client"], '
                                    '["controller", "client"])'
                                ),
                            },
                            "nic_speed": {
                                "type": ["integer", "string"],
                                "description": (
                                    "Required NIC speed in Gbps (e.g. 25, '100Gbps')"
                                ),
                            },
                            "min_cores": {
                                "type": "integer",
                                "description": "Minimum CPU cores",
                            },
                            "min_memory_gb": {
                                "type": "integer",
                                "description": "Minimum RAM in GB",
                            },
                            "os": {
                                "type": "string",
                                "description": "OS requirement (e.g. 'RHEL9')",
                            },
                            "host": {
                                "type": "string",
                                "minLength": 1,
                                "description": (
                                    "Exact FQDN or IP of an existing host "
                                    "the user named, copied VERBATIM "
                                    "(case, domain suffixes). Omit for "
                                    "hosts a provider will allocate."
                                ),
                            },
                        },
                        "required": ["roles"],
                    },
                    "description": (
                        "Every host needed for the test, each with its "
                        "roles and optional hardware requirements. "
                        "Always include a controller. A host can serve "
                        "multiple roles (e.g. controller + client). "
                        "Attach hardware specs the user requested to "
                        "the relevant host entries. When the user names "
                        "specific existing hosts, set 'host' to the "
                        "exact FQDN or IP — never paraphrase, truncate, "
                        "or resolve. "
                        "Example: [{roles: [controller], host: "
                        "'ctrl-01.lab.example.com'}, "
                        "{roles: [client], nic_speed: 25, os: 'RHEL9'}, "
                        "{roles: [server], host: "
                        "'node-42.lab.example.com'}]"
                    ),
                },
                "directives": {
                    "type": "object",
                    "description": (
                        "Operational directives extracted from the user's "
                        "request. Only include directives the user explicitly "
                        "or clearly implied. Omit any directive that was not "
                        "mentioned."
                    ),
                    "properties": {
                        "on_existing_install": {
                            "type": "string",
                            "enum": [
                                "reinstall",
                                "update",
                                "skip",
                                "ask_user",
                            ],
                            "description": (
                                "What to do if the harness is already "
                                "installed. 'reinstall' = uninstall then "
                                "clean install, 'update' = update in place, "
                                "'skip' = use existing installation, "
                                "'ask_user' = ask the user what to do."
                            ),
                        },
                        "harness": {
                            "type": "string",
                            "description": (
                                "Which benchmark harness to use (e.g. "
                                "'crucible', 'zathras'). Only set if the "
                                "user explicitly names a harness."
                            ),
                        },
                        "user_pre_run_approval": {
                            "type": "boolean",
                            "description": (
                                "Whether to ask the user for approval "
                                "before starting the benchmark run. "
                                "Defaults to true if not specified. Set "
                                "to false if the user says something like "
                                "'don't ask me for approval' or 'just run it'."
                            ),
                        },
                        "host_cleanup": {
                            "type": "string",
                            "enum": ["required", "skip"],
                            "description": (
                                "Whether to clean up SSH keys and harness "
                                "installations from hosts during teardown. "
                                "Default: required."
                            ),
                        },
                        "endpoint_type": {
                            "type": "string",
                            "enum": ["remotehosts", "kube"],
                            "description": (
                                "Endpoint type for the benchmark. "
                                "'remotehosts' runs directly on "
                                "bare-metal/VM hosts. 'kube' runs in "
                                "Kubernetes pods (K3s installed on the "
                                "controller). Set to 'kube' when user "
                                "mentions Kubernetes, K8s, pods, "
                                "containers, or cloud-native."
                            ),
                        },
                        "workflow_source": {
                            "type": "string",
                            "description": (
                                "Git repo URL or raw workflow file URL "
                                "for Arcaflow workflow execution. When "
                                "set, the benchmark agent uses the "
                                "Arcaflow MCP to load, configure, and "
                                "run the workflow instead of direct "
                                "plugin execution. Example: "
                                "'https://gitlab.com/org/repo.git'"
                            ),
                        },
                        "workflow_name": {
                            "type": "string",
                            "description": (
                                "Name or path of the workflow within "
                                "the workflow_source repo. Only needed "
                                "when the source contains multiple "
                                "workflows. Example: 'workflow-fio'"
                            ),
                        },
                    },
                    "additionalProperties": True,
                },
                "execution_plan": {
                    "type": "array",
                    "description": (
                        "Optional multi-step execution plan. Include "
                        "when the user's request requires multiple "
                        "benchmark runs or different infrastructure "
                        "per iteration. Each step specifies an "
                        "agent_type and params. The final step "
                        "should be 'review'."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_type": {
                                "type": "string",
                                "enum": [
                                    "teardown",
                                    "resource",
                                    "provision",
                                    "benchmark",
                                    "review",
                                ],
                            },
                            "params": {
                                "type": "object",
                                "description": (
                                    "Step-specific params. For benchmark: "
                                    "label and mv_params overrides. "
                                    "For resource: required_hosts list "
                                    "and optional directives overrides. "
                                    "For teardown/provision/review: empty."
                                ),
                            },
                        },
                        "required": ["agent_type", "params"],
                    },
                },
                "scoped_context": {
                    "type": "object",
                    "description": (
                        "Agent-scoped context partitioned from the user's "
                        "request. Each key is an agent role ('resource', "
                        "'provision', 'benchmark', 'review') or 'shared' "
                        "for context relevant to all agents. "
                        "IMPORTANT: if the ticket contains a verbatim "
                        "agent:* fenced block for a key (shown in "
                        "'Pre-parsed Verbatim Directives' above), that "
                        "block is already delivered to the agent verbatim "
                        "— do NOT restate, rephrase, reformat, or "
                        "summarize any of its content here, in any form "
                        "(not as prose, not as a numbered list, not as "
                        "bullet points). Reformatting is still restating. "
                        "Only write a value for that key if you have "
                        "information from OTHER parts of the ticket that "
                        "the verbatim block does not mention at all — "
                        "for example: a directive resolved from the "
                        "ticket's custom_fields (like on_existing_install), "
                        "a constraint inferred from the host inventory, or "
                        "an ambiguity you resolved. If the only thing you "
                        "could write is a restatement of the verbatim "
                        "block, omit the key entirely."
                    ),
                    "properties": {
                        "shared": {
                            "type": "string",
                            "description": (
                                "Context relevant to all agents "
                                "(environment, general constraints, "
                                "test objective summary). Do NOT use "
                                "geographic labels or shorthand in place "
                                "of hostnames — always use the exact "
                                "FQDNs or IPs from the ticket."
                            ),
                        },
                        "resource": {
                            "type": "string",
                            "description": (
                                "Context for the resource agent. REQUIRED "
                                "whenever required_hosts is non-empty. "
                                "For user-provided hosts: quote every "
                                "hostname, FQDN, and IP address VERBATIM "
                                "from the ticket — do NOT paraphrase as "
                                "geographic labels (e.g. 'BOS controller') "
                                "or invent shorthand names. The resource "
                                "agent uses these exact strings to SSH into "
                                "the hosts; a paraphrase causes it to "
                                "invent bogus hostnames. "
                                "For cloud/QUADS: include provider, "
                                "instance type, region, and any constraints "
                                "the user specified."
                            ),
                        },
                        "provision": {
                            "type": "string",
                            "description": (
                                "Supplemental context for the provision "
                                "agent — only if genuinely additive beyond "
                                "any verbatim agent:provision block. Include "
                                "a package only when the user explicitly "
                                "requested it or the harness platform contract "
                                "requires it; benchmark tool-params never imply "
                                "a host package requirement."
                            ),
                        },
                        "benchmark": {
                            "type": "string",
                            "description": (
                                "Supplemental context for the benchmark "
                                "agent — only if genuinely additive beyond "
                                "any verbatim agent:benchmark block."
                            ),
                        },
                        "review": {
                            "type": "string",
                            "description": (
                                "Supplemental context for the review "
                                "agent — only if genuinely additive beyond "
                                "any verbatim agent:review block."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
                "reference_tickets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ticket IDs referenced for comparison or "
                        "context (e.g. ['PERF-ABC123', 'PERF-DEF456']). "
                        "Set when the user asks to compare or reference "
                        "prior investigation results."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": "Additional notes about the triage",
                },
                "fleet_investigation": {
                    "type": "boolean",
                    "description": (
                        "Set to true when the user requests testing "
                        "across multiple boards/hosts of the same type "
                        "(e.g. 'test all S32G boards', 'fleet-wide "
                        "boot time', 'compare across boards'). "
                        "Each board will be tested individually and "
                        "results compared."
                    ),
                },
                "image_build": {
                    "type": "object",
                    "description": (
                        "Custom image build specification. "
                        "Set when the user requests a custom "
                        "OS image with package overrides, "
                        "service changes, or specific build "
                        "parameters. The system will build "
                        "the image before acquiring hardware."
                    ),
                    "properties": {
                        "provider": {
                            "type": "string",
                            "description": (
                                "Image build provider name. Omit to use the default."
                            ),
                        },
                        "target": {
                            "type": "string",
                            "description": (
                                "Build target platform. Omit "
                                "to auto-resolve from "
                                "board_selector."
                            ),
                        },
                        "customizations": {
                            "type": "object",
                            "description": (
                                "Image customizations: rpms, "
                                "repos, enabled_services, "
                                "disabled_services, "
                                "masked_services, kernel"
                            ),
                        },
                    },
                },
            },
            "required": [
                "parsed_specs",
                "hypothesis",
                "benchmark_suite",
                "absent_suite",
                "required_hosts",
            ],
        },
    ),
]


def merge_image_build(
    user_build: dict[str, Any],
    triage_build: dict[str, Any],
) -> dict[str, Any]:
    """Merge triage image_build with user-provided values.

    User-provided fields take precedence. Customizations are
    merged deeply so both user and triage customizations are
    preserved.
    """
    merged = {**triage_build, **user_build}
    if "customizations" in triage_build and "customizations" in user_build:
        merged["customizations"] = {
            **triage_build["customizations"],
            **user_build["customizations"],
        }
    return merged


# Month names → two-digit month number for release date extraction.
_MONTH_NAMES: dict[str, str] = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}

# Matches month names (full or common 3-letter abbreviation)
# optionally followed by a 4-digit year, or a standalone
# YYYYMM+ datestamp (6-12 digits starting with 20xx).
# Used to detect when a ticket description references a
# specific date but triage emitted a bare "monthly" release.
_MONTH_NAME_PATTERN = "|".join(
    rf"(?:{month[:3]}(?:{re.escape(month[3:])})?)" for month in _MONTH_NAMES
)
_DATE_REFERENCE_RE = re.compile(
    r"\bmonthly\s+from\s+(?:"
    + _MONTH_NAME_PATTERN
    + r")\b(?:\s+\d{4})?"
    + r"|\b(?:"
    + _MONTH_NAME_PATTERN
    + r")\b(?:\s+\d{4}|\s+(?:monthly|build|image|release))"
    + r"|\b20\d{2}(?:0[1-9]|1[0-2])\d{0,8}\b",
    re.IGNORECASE,
)


def _warn_bare_monthly_release(
    description: str,
    release: str,
) -> str | None:
    """Return a warning string if the description mentions a specific
    month or date but the release directive is a bare ``"monthly"``
    without a date qualifier.

    Returns ``None`` when no warning is needed.
    """
    if release != "monthly":
        return None
    match = _DATE_REFERENCE_RE.search(description)
    if not match:
        return None
    ref = match.group(0).strip()
    return (
        f"\u26a0\ufe0f **Release date mismatch:** The description "
        f'mentions "{ref}" but the release directive is bare '
        f'`"monthly"`, which resolves to the **latest** monthly '
        f"build. If a specific month was intended, the release "
        f"should include a date qualifier "
        f"(e.g., `monthly/autosd10-YYYYMM`)."
    )


class TriageAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider,
        event_bus: EventBus | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._ticket_id: str | None = None

        local_tools = list(_LOCAL_TOOLS)

        async def _request_clarification(question: str) -> str:
            return await self._do_request_clarification(question)

        local_handlers = {
            "request_clarification": _request_clarification,
        }

        super().__init__(
            agent_name="triage-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers=local_handlers,
            event_bus=event_bus,
        )

    async def _do_request_clarification(self, question: str) -> str:
        if self._ticket_id:
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    async def run(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

        triage_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect_ticket_server(
            triage_server,
            name="triage",
            ticket_id=ticket_id,
            state_store_url=self.store_url,
            agent_name=self.agent_name,
        )
        await mcp.connect_ticket_server(
            infra_server,
            name="infra",
            ticket_id=ticket_id,
            state_store_url=self.store_url,
            agent_name=self.agent_name,
        )

        # Triage may use explicitly authorized external discovery tools (for
        # example Arcaflow's plugin_list). Keep those servers scoped exactly
        # as configured; never expose an unconfigured external tool to the
        # triage LLM.
        from agents.mcp_client import connect_external_servers, filter_external_tools

        connected_ext, ext_tools = await connect_external_servers(mcp, "triage")
        self._mcp = mcp

        mcp_tools = await mcp.list_tools()
        if ext_tools:
            mcp_tools = filter_external_tools(
                mcp_tools, mcp._tool_routing, connected_ext, ext_tools
            )
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return TRIAGE_SYSTEM_PROMPT

    def _has_external_data_tools(self) -> bool:
        """Check if external MCP data tools are configured."""
        from orchestrator.config import _load_config_file

        try:
            config = _load_config_file()
        except Exception:
            return False
        servers = config.get("external_mcp_servers", [])
        for srv in servers:
            agents = srv.get("agents", {})
            if "analyze" in agents or "gathering_context" in agents:
                return True
        return False

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        # Skip Summary when it duplicates the description (cli.py submit
        # uses description as summary when no -d flag is given).
        summary = ticket["summary"]
        description = ticket["description"]
        if summary == description or description.startswith(summary):
            header = (
                f"**Ticket ID:** {ticket['id']}\n\n**Description:**\n{description}\n"
            )
        else:
            header = (
                f"**Ticket ID:** {ticket['id']}\n"
                f"**Summary:** {summary}\n\n"
                f"**Description:**\n{description}\n"
            )
        content = f"## Performance Test Request\n\n{header}"

        # Tell triage whether external data tools are available
        has_data_tools = self._has_external_data_tools()
        cf = ticket.get("custom_fields", {})
        has_anomaly = bool(cf.get("anomaly_context"))
        ref_tickets = cf.get("reference_tickets", [])

        content += "\n## Data Analysis Capability\n\n"
        if has_data_tools:
            content += (
                "External data tools ARE available. You CAN "
                "include an `analyze` step in the execution plan "
                "when the investigation would benefit from "
                "analyzing existing data before provisioning "
                "hardware.\n"
            )
        else:
            content += (
                "External data tools are NOT available. Do NOT "
                "include an `analyze` step — use the standard "
                "benchmark-first plan.\n"
            )
        if has_anomaly:
            content += (
                "\nThis ticket has anomaly_context (alert-triggered). "
                "It will route through gathering_context for dedup "
                "before reaching the execution plan.\n"
            )
        if ref_tickets:
            content += (
                f"\nReference tickets provided: {ref_tickets}. "
                "Include an analyze step for cross-ticket "
                "comparison.\n"
            )

        # Surface existing directives so the LLM knows
        # what the user already specified (e.g., workflow_source,
        # board_selector set at ticket creation).
        existing_directives = cf.get("directives", {})
        if existing_directives:
            content += "\n## User-Provided Directives\n\n"
            content += (
                "These directives were set at ticket creation. "
                "Include them in your submit_triage_result "
                "directives \u2014 do NOT ask the user to provide "
                "information that is already here.\n\n"
                f"```json\n{json.dumps(existing_directives, indent=2)}\n```\n"
            )

        _TRIAGE_NOISE_AUTHORS = frozenset({"system", "orchestrator"})
        relevant_comments = [
            c
            for c in (ticket.get("comments") or [])
            if c.get("author") not in _TRIAGE_NOISE_AUTHORS
        ]
        if relevant_comments:
            content += "\n## Previous Comments\n"
            for comment in relevant_comments:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        cf = ticket.get("custom_fields", {})
        verbatim_directives = cf.get("verbatim_directives") or {}
        if verbatim_directives:
            content += "\n## Pre-parsed Verbatim Directives\n\n"
            content += (
                "The following directives were extracted verbatim from the "
                "ticket description and will be delivered directly to each "
                "target agent. Do NOT summarize or paraphrase these in "
                "`scoped_context` — your `scoped_context` entries should "
                "contain only supplemental context that these blocks do not "
                "already cover.\n\n"
            )
            for target, text in verbatim_directives.items():
                content += f"**agent:{target}:**\n```\n{text}\n```\n\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            await self._add_comment(
                ticket_id, "Triage agent could not produce structured output."
            )
            return

        required_hosts = result.get("required_hosts", [])
        directives = result.get("directives", {})
        # Backward compat: top-level host_cleanup moves into directives
        if "host_cleanup" in result and "host_cleanup" not in directives:
            directives["host_cleanup"] = result["host_cleanup"]
        # Preserve user-provided directives that triage
        # didn't set. The user may have specified
        # image_version or other operational parameters
        # in the ticket's custom_fields.
        # User directives take precedence over triage's
        # — triage fills gaps, doesn't override.
        # Also check top-level custom_fields for directives
        # that the user set outside the directives dict
        # (e.g., image_version, system_config).
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        user_directives = cf.get("directives", {})
        if user_directives:
            merged = dict(directives)
            merged.update(user_directives)
            directives = merged
        # Promote top-level custom_fields that belong
        # in directives but weren't placed there by
        # the ticket creator.
        _PROMOTABLE = (
            "image_version",
            "serial_capture",
            "board_selector",
            "workflow_source",
            "workflow_name",
        )
        for key in _PROMOTABLE:
            if key in cf and key not in directives:
                directives[key] = cf[key]
        # Resolve the canonical harness name.  _effective_harness
        # handles workflow_source → arcaflow-workflows mapping and
        # alias normalization.  The canonical name is written back
        # to directives so downstream agents and the LLM see a
        # consistent, resolvable value.
        harness = self._effective_harness(directives, self._skill_provider)
        # Store the canonical harness name so downstream agents
        # and the LLM see a consistent, resolvable value.
        if harness and harness != directives.get("harness"):
            directives["harness"] = harness

        harness_intent = _description_harness_intent(
            ticket.get("description") or "",
            _known_harness_names(self._skill_provider),
        )
        requested_harnesses = set(harness_intent.required)
        if user_directives.get("harness"):
            requested_harnesses.add(
                self._effective_harness(
                    {"harness": user_directives["harness"]},
                    self._skill_provider,
                )
            )
        harness_intent = _HarnessIntent(
            required=frozenset(requested_harnesses),
            alternatives=harness_intent.alternatives,
            excluded=harness_intent.excluded,
        )

        async def pause_for_harness_guidance(message: str) -> None:
            await self._add_comment(
                ticket_id,
                f"**Triage validation failed:** {message}",
            )
            await self._transition_ticket(
                ticket_id,
                "awaiting_customer_guidance",
                comment="Triage validation failed; awaiting harness guidance.",
            )

        intent_conflict = _harness_intent_conflict(harness_intent, harness)
        if intent_conflict:
            await pause_for_harness_guidance(
                f"{intent_conflict} Please clarify which harness to use."
            )
            return

        # --- Harness / suite validation (issue #1086) ---
        # Code-enforced: reject or auto-correct invalid harness names
        # and non-existent benchmark suites before they propagate.
        benchmark_name = result.get("benchmark_suite", "")
        absent_suite = result.get("absent_suite", False)
        validation = await _validate_harness_and_suite(
            harness, benchmark_name, absent_suite, self._skill_provider
        )
        if validation is not None:
            if "error" in validation:
                # Hard reject — harness doesn't exist at all.
                await self._add_comment(
                    ticket_id,
                    f"**Triage validation failed:** {validation['error']}\n\n"
                    f"Please update the ticket with a valid harness name.",
                )
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_customer_guidance",
                    comment=("Triage validation failed; waiting for a valid harness."),
                )
                return
            if "correction" in validation:
                correction = validation["correction"]
                catalog_harness = correction.get("harness")
                correction_conflict = (
                    _harness_intent_conflict(harness_intent, catalog_harness)
                    if catalog_harness
                    else None
                )
                if correction_conflict:
                    corrected_suite = correction.get("benchmark_suite", benchmark_name)
                    if len(harness_intent.required) == 1 and catalog_harness != next(
                        iter(harness_intent.required)
                    ):
                        requested_harness = next(iter(harness_intent.required))
                        conflict_message = (
                            f"The requested harness '{requested_harness}' does not "
                            f"match the catalog harness '{catalog_harness}' for "
                            f"benchmark suite '{corrected_suite}'. Please update the "
                            "harness or benchmark suite to continue."
                        )
                    else:
                        conflict_message = (
                            f"The catalog harness '{catalog_harness}' conflicts "
                            f"with the request: {correction_conflict} Please update "
                            "the harness or benchmark suite to continue."
                        )
                    await pause_for_harness_guidance(conflict_message)
                    return
                if "benchmark_suite" in correction:
                    result["benchmark_suite"] = correction["benchmark_suite"]
                    benchmark_name = correction["benchmark_suite"]
                if "absent_suite" in correction:
                    result["absent_suite"] = correction["absent_suite"]
                if "harness" in correction:
                    harness = correction["harness"]
                    directives["harness"] = harness
                note = correction.get("note", "")
                if note:
                    existing_notes = result.get("notes", "")
                    result["notes"] = (
                        f"{existing_notes}\n{note}" if existing_notes else note
                    )
                    logger.info("[triage] %s", note)

        # Resolve execution model from the harness metadata.
        # This is a harness-level property declared in BenchmarkSuite,
        # not a per-ticket decision or a hardcoded list.
        from providers.skills.base import EXECUTION_MODEL_DIRECT
        from providers.skills.catalog import resolve_execution_model

        execution_model = await resolve_execution_model(
            self._skill_provider, harness, benchmark_name
        )

        # Direct harnesses need only target hosts — no controller.
        # The LLM often includes a controller role which causes the
        # resource agent to allocate a non-existent controller host.
        if execution_model == EXECUTION_MODEL_DIRECT:
            required_hosts = _filter_direct_required_hosts(required_hosts)

        fields: dict[str, Any] = {
            "parsed_specs": result.get("parsed_specs", {}),
            "hypothesis": result.get("hypothesis", ""),
            "benchmark_suite": result.get("benchmark_suite", ""),
            "absent_suite": result.get("absent_suite", False),
            "required_hosts": required_hosts,
            "directives": directives,
            "execution_model": execution_model,
        }
        if hasattr(self._skill_provider, "get_source_provenance"):
            source_provenance = self._skill_provider.get_source_provenance("crucible")
            if source_provenance:
                fields["crucible_source"] = source_provenance

        scoped_context = result.get("scoped_context")
        if scoped_context and isinstance(scoped_context, dict):
            unknown = set(scoped_context) - _KNOWN_SCOPED_CONTEXT_KEYS
            if unknown:
                logger.warning(
                    "scoped_context contains unknown keys %s — dropping",
                    sorted(unknown),
                )
                scoped_context = {
                    k: v
                    for k, v in scoped_context.items()
                    if k in _KNOWN_SCOPED_CONTEXT_KEYS
                }
            fields["scoped_context"] = scoped_context

        reference_tickets = result.get("reference_tickets")
        if reference_tickets and isinstance(reference_tickets, list):
            fields["reference_tickets"] = reference_tickets

        # Every ticket gets a full-lifecycle execution plan covering
        # resource allocation through teardown. The LLM should
        # produce this, but if it doesn't, we build a default.
        raw_plan = result.get("execution_plan")
        if raw_plan and isinstance(raw_plan, list) and len(raw_plan) > 0:
            steps = []
            for i, s in enumerate(raw_plan):
                steps.append(
                    {
                        "id": i,
                        "agent_type": s.get("agent_type", "benchmark"),
                        "status": "in_progress" if i == 0 else "pending",
                        "params": s.get("params", {}),
                        "results": {},
                    }
                )
            # The resource agent validates SSH, inventories hosts, and
            # populates assigned_hardware_ips.  If the LLM omitted it
            # (e.g. user-provided hosts), prepend one so the pipeline
            # doesn't skip host validation.  "analyze" is the only
            # valid non-resource first step (data-only investigation).
            if steps[0]["agent_type"] not in ("resource", "analyze"):
                steps.insert(
                    0,
                    {
                        "id": 0,
                        "agent_type": "resource",
                        "status": "in_progress",
                        "params": {},
                        "results": {},
                    },
                )
                for i, s in enumerate(steps):
                    s["id"] = i
                    s["status"] = "in_progress" if i == 0 else "pending"
        else:
            # Default full-lifecycle plan
            steps = [
                {
                    "id": 0,
                    "agent_type": "resource",
                    "status": "in_progress",
                    "params": {},
                    "results": {},
                },
                {
                    "id": 1,
                    "agent_type": "provision",
                    "status": "pending",
                    "params": {},
                    "results": {},
                },
                {
                    "id": 2,
                    "agent_type": "benchmark",
                    "status": "pending",
                    "params": {},
                    "results": {},
                },
                {
                    "id": 3,
                    "agent_type": "review",
                    "status": "pending",
                    "params": {},
                    "results": {},
                },
                {
                    "id": 4,
                    "agent_type": "teardown",
                    "status": "pending",
                    "params": {},
                    "results": {},
                },
            ]
        fields["execution_plan"] = {
            "current_step": 0,
            "run_ids": [],
            "steps": steps,
        }

        # Apply step 0's overrides directly — _apply_step_overrides
        # handles this for subsequent steps, but step 0 runs before
        # any plan advancement.
        first_params = steps[0].get("params", {})
        first_type = steps[0]["agent_type"]

        # Step 0's required_hosts override the ticket-level list
        if first_type == "resource" and first_params.get("required_hosts"):
            fields["required_hosts"] = first_params["required_hosts"]

        # Apply step 0's per-step scoped_context if provided,
        # mirroring _apply_step_overrides for later steps.
        # Do NOT clear ticket-level scoped_context for step 0 —
        # it was written moments earlier by this same triage run
        # and cannot be stale.  Clearing only applies to steps ≥ 1
        # (multi-iteration text from prior runs).
        if first_params.get("scoped_context"):
            fields.setdefault("scoped_context", {}).update(
                first_params["scoped_context"]
            )

        # Fleet investigation: set up tracking state if
        # the triage agent detected a fleet request or the
        # user explicitly set fleet_investigation in cf.
        is_fleet = result.get("fleet_investigation", False)
        if not is_fleet:
            is_fleet = cf.get("fleet_investigation", {}).get("enabled", False)
        if is_fleet:
            existing_fleet = cf.get("fleet_investigation", {})
            fields["fleet_investigation"] = {
                **existing_fleet,
                "enabled": True,
                "tested_hosts": existing_fleet.get("tested_hosts", []),
            }
        # Custom image build: store spec and prepend
        # a build_image step to the execution plan.
        # Merge triage image_build with user-provided values;
        # user-provided fields take precedence.
        # Do NOT add image_build if the user didn't request one
        # and system_config is present — system_config means
        # post-flash configuration, not a custom build.
        image_build = cf.get("image_build", {})
        triage_build = result.get("image_build", {})
        if triage_build and not image_build:
            # Triage inferred a build the user didn't request.
            # Block if system_config is present.
            if cf.get("system_config"):
                logger.info(
                    "[triage] Ignoring triage-inferred "
                    "image_build — system_config present"
                )
                triage_build = {}
        if triage_build or image_build:
            fields["image_build"] = merge_image_build(image_build, triage_build)
            # Prepend build step before resource step
            plan = fields.get("execution_plan", {})
            steps = plan.get("steps", [])
            build_step = {
                "id": 0,
                "agent_type": "build_image",
                "status": "in_progress",
                "params": {},
                "results": {},
            }
            # Renumber existing steps
            for i, s in enumerate(steps):
                s["id"] = i + 1
                if i == 0:
                    s["status"] = "pending"
            steps.insert(0, build_step)
            plan["steps"] = steps
            plan["current_step"] = 0
            fields["execution_plan"] = plan

        await self._update_fields(ticket_id, fields)

        summary = (
            f"**Triage Complete**\n\n"
            f"- **Hypothesis:** {fields['hypothesis']}\n"
            f"- **Benchmark Suite:** {fields['benchmark_suite']}\n"
            f"- **Required Hosts:** {len(required_hosts)} ({', '.join('+'.join(h.get('roles', ['?'])) for h in required_hosts)})\n"
            f"- **Absent Suite:** {fields['absent_suite']}\n"
        )
        step_types = [s["agent_type"] for s in steps]
        summary += (
            f"- **Execution Plan:** {len(steps)} steps ({' → '.join(step_types)})\n"
        )
        if directives:
            summary += f"- **Directives:** {', '.join(f'{k}={v}' for k, v in directives.items())}\n"
        if fields.get("scoped_context"):
            agents_with_context = [
                k
                for k in fields["scoped_context"]
                if k != "shared" and fields["scoped_context"].get(k)
            ]
            if agents_with_context:
                summary += f"- **Scoped Context:** {', '.join(agents_with_context)}\n"
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        # Warn if the description mentions a specific month/date
        # but triage produced a bare "monthly" release directive.
        release_warning = _warn_bare_monthly_release(
            ticket.get("description", ""),
            directives.get("release", ""),
        )
        if release_warning:
            summary += f"\n{release_warning}\n"

        await self._add_comment(ticket_id, summary)

        # Route based on whether anomaly_context is present.
        # Set by alert seeds, CLI, or API — not inferred by
        # the LLM. Code enforces the routing invariant.
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        if cf.get("anomaly_context"):
            await self._transition_ticket(
                ticket_id,
                "gathering_context",
                comment=(
                    "Triage complete, anomaly context present"
                    " — routing to investigation"
                ),
            )
        else:
            # Only "resource" and "analyze" have valid transitions
            # from triage_pending.  Any other step type (e.g. the LLM
            # skipping "resource" and putting "provision" first) must
            # funnel through awaiting_hardware so the resource agent
            # still validates hosts and populates assigned_hardware_ips.
            _TRIAGE_EXIT_STATUSES = {
                "resource": "awaiting_hardware",
                "analyze": "analyzing",
                "build_image": "building_image",
            }
            first_step_type = steps[0]["agent_type"]
            first_status = _TRIAGE_EXIT_STATUSES.get(
                first_step_type,
                "awaiting_hardware",
            )
            await self._transition_ticket(
                ticket_id,
                first_status,
                comment=f"Triage complete, starting plan step 0: {first_step_type}",
            )
