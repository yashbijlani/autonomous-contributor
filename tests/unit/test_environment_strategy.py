"""Unit: EnvironmentStrategy selection + CI failure classification."""
from contributor.config import Settings
from contributor.github.ci import classify_ci_failure
from contributor.models.state import (
    CIFailureClass,
    CIResult,
    EnvironmentReport,
    EnvironmentStrategy,
)
from contributor.sandbox.strategy import choose_strategy, is_container_compatible


def test_choose_strategy_variants():
    assert choose_strategy(EnvironmentReport()) == EnvironmentStrategy.STANDARD_DOCKER
    assert choose_strategy(EnvironmentReport(signals=["dockerfile"])) == EnvironmentStrategy.CUSTOM_DOCKER
    assert choose_strategy(EnvironmentReport(signals=["devcontainer"])) == EnvironmentStrategy.REPOSITORY_DEVCONTAINER
    assert choose_strategy(EnvironmentReport(requires_systemd=True)) == EnvironmentStrategy.HOST_REQUIRED
    assert choose_strategy(EnvironmentReport(requires_host_runtime=True)) == EnvironmentStrategy.HOST_REQUIRED
    assert choose_strategy(EnvironmentReport(unsafe_ops=True, requires_host_runtime=True)) == EnvironmentStrategy.INCOMPATIBLE


def test_container_compatibility():
    assert is_container_compatible(EnvironmentStrategy.STANDARD_DOCKER)
    assert is_container_compatible(EnvironmentStrategy.CUSTOM_DOCKER)
    assert not is_container_compatible(EnvironmentStrategy.HOST_REQUIRED)
    assert not is_container_compatible(EnvironmentStrategy.INCOMPATIBLE)


def _ci(names: list[str]) -> CIResult:
    return CIResult(state="fail", checks=[{"name": n, "state": "fail"} for n in names], summary="fail")


def test_ci_code_failure():
    assert classify_ci_failure(_ci(["unit tests"])) == CIFailureClass.CODE_FAILURE


def test_ci_dependency_failure():
    assert classify_ci_failure(_ci(["setup-python missing dependency"])) == CIFailureClass.DEPENDENCY_FAILURE


def test_ci_environment_failure():
    assert classify_ci_failure(_ci(["workspace toolchain missing"])) == CIFailureClass.ENVIRONMENT_FAILURE


def test_ci_infrastructure_failure():
    assert classify_ci_failure(_ci(["runner startup cancelled"])) == CIFailureClass.CI_INFRASTRUCTURE_FAILURE


def test_ci_env_report_overrides():
    rep = EnvironmentReport(container_compatible=False, strategy=EnvironmentStrategy.HOST_REQUIRED)
    assert classify_ci_failure(_ci(["unit tests"]), rep) == CIFailureClass.ENVIRONMENT_FAILURE


def test_ci_unknown():
    assert classify_ci_failure(_ci(["something odd"])) == CIFailureClass.UNKNOWN
