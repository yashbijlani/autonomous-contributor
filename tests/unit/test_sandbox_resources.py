"""Unit: resource profile selection + host capping."""
from contributor.models.state import EnvironmentReport
from contributor.sandbox.resources import (
    choose_resource_profile,
    parse_memory,
)


def test_rust_selects_heavy_profile():
    r = EnvironmentReport(languages=["rust"], package_managers=["cargo"], test_frameworks=["cargo-test"])
    sel = choose_resource_profile(r, host=(4.0, 6 * 1024**3, 3 * 1024**3))
    assert sel.profile.name in ("medium", "large")
    assert sel.compatible


def test_requested_profile_is_capped_by_host():
    r = EnvironmentReport(languages=["rust"])
    sel = choose_resource_profile(r, requested="large", host=(2.0, 4 * 1024**3, 1 * 1024**3))
    assert sel.profile.cpus <= 2.0
    assert parse_memory(sel.profile.memory) <= int(4 * 1024**3 * 0.85)
    assert any("capped" in w for w in sel.warnings)


def test_resource_incompatible_when_host_too_small():
    r = EnvironmentReport(languages=["rust"])
    sel = choose_resource_profile(r, host=(2.0, 1 * 1024**3, 1 * 1024**3))
    assert not sel.compatible
    assert any("insufficient" in x for x in sel.reasons)


def test_light_python_project_uses_small():
    r = EnvironmentReport(languages=["python"], package_managers=[])
    sel = choose_resource_profile(r, host=(8.0, 32 * 1024**3, 16 * 1024**3))
    assert sel.profile.name == "small"


def test_parse_memory():
    assert parse_memory("2g") == 2 * 1024**3
    assert parse_memory("512m") == 512 * 1024**2
