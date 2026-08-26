from __future__ import annotations

"""Abstract interface for image build providers.

Each provider implements build() which takes a build spec
from ticket directives and returns a result with the image
URL for flashing.
"""


from dataclasses import dataclass, field
from typing import Any


@dataclass
class BuildSpec:
    """Input specification for an image build."""

    name: str = ""
    target: str = ""
    customizations: dict[str, Any] = field(default_factory=dict)
    timeout_minutes: int = 60
    extra_options: dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildResult:
    """Output from a completed image build."""

    success: bool = False
    build_name: str = ""
    image_url: str = ""
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ImageBuildProvider:
    """Abstract image build provider.

    Subclasses implement build() for their specific build
    system (CAIB, Koji, OSBS, etc.).
    """

    provider_name: str = "unknown"

    def resolve_target(self, board_selector: str) -> str:
        """Resolve a board selector to a build target.

        Override in subclasses to map board selectors
        (e.g., 'board-type=qc8775') to build targets
        (e.g., 'ride4_sa8775p_sx').
        """
        return "default"

    def resolve_build_mode(self, board_selector: str) -> str:
        """Resolve a board selector to a build mode.

        Override in subclasses for provider-specific modes.
        """
        return "default"

    async def build(self, spec: BuildSpec) -> BuildResult:
        """Build an image from the given spec.

        Args:
            spec: Build specification with target, name,
                and customizations.

        Returns:
            BuildResult with image_url on success or
            error message on failure.
        """
        raise NotImplementedError
