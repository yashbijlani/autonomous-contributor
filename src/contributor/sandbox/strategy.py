"""EnvironmentStrategy: decide how a repository must be executed.

Security rule: never weaken isolation to make tests pass. Repositories that
need systemd, host services, special hardware, or privileged operations are
classified HOST_REQUIRED/INCOMPATIBLE rather than granted capabilities.
"""
from __future__ import annotations

from contributor.config import Settings
from contributor.models.state import EnvironmentReport, EnvironmentStrategy


def choose_strategy(report: EnvironmentReport, settings: Settings | None = None) -> EnvironmentStrategy:
    """Deterministic strategy selection from the environment report."""
    if report.unsafe_ops and report.requires_host_runtime:
        return EnvironmentStrategy.INCOMPATIBLE
    if report.requires_systemd or report.requires_host_runtime:
        return EnvironmentStrategy.HOST_REQUIRED
    if "devcontainer" in report.signals:
        return EnvironmentStrategy.REPOSITORY_DEVCONTAINER
    if "dockerfile" in report.signals:
        return EnvironmentStrategy.CUSTOM_DOCKER
    return EnvironmentStrategy.STANDARD_DOCKER


def is_container_compatible(strategy: EnvironmentStrategy) -> bool:
    return strategy in (
        EnvironmentStrategy.STANDARD_DOCKER,
        EnvironmentStrategy.CUSTOM_DOCKER,
        EnvironmentStrategy.REPOSITORY_DEVCONTAINER,
        EnvironmentStrategy.REPOSITORY_CI_ENVIRONMENT,
    )
