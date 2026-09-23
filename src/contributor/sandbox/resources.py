"""Resource profiles: choose sane CPU/memory for a repository build, capped by
host capacity. Never exceeds the host; reports RESOURCE_INCOMPATIBLE when the
required build cannot fit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from contributor.models.state import EnvironmentReport


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    cpus: float
    memory: str  # docker memory string, e.g. "4g"

    @property
    def memory_bytes(self) -> int:
        return parse_memory(self.memory)


SMALL = ResourceProfile("small", 2.0, "3g")
MEDIUM = ResourceProfile("medium", 4.0, "6g")
LARGE = ResourceProfile("large", 8.0, "12g")
PROFILES = {"small": SMALL, "medium": MEDIUM, "large": LARGE}

# Ecosystems whose builds are memory-hungry.
HEAVY_LANGUAGES = {"rust", "java", "cpp", "c", "kotlin", "scala", "swift"}


def parse_memory(value: str) -> int:
    v = value.strip().lower()
    try:
        if v.endswith("g"):
            return int(float(v[:-1]) * 1024**3)
        if v.endswith("m"):
            return int(float(v[:-1]) * 1024**2)
        if v.endswith("k"):
            return int(float(v[:-1]) * 1024)
        return int(float(v))
    except Exception:
        return 0


def _read_meminfo() -> tuple[int, int]:
    total = avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
                if total and avail:
                    break
    except Exception:
        pass
    return total, avail


def host_capacity() -> tuple[float, int, int]:
    """Return (cpus, total_bytes, available_bytes)."""
    cpus = float(os.cpu_count() or 1)
    total, avail = _read_meminfo()
    if total == 0:
        total = 2 * 1024**3
    if avail == 0:
        avail = total
    return cpus, total, avail


@dataclass
class ResourceSelection:
    profile: ResourceProfile
    compatible: bool
    warnings: list[str]
    reasons: list[str]


def choose_resource_profile(
    report: EnvironmentReport,
    *,
    requested: str = "auto",
    host: tuple[float, int, int] | None = None,
) -> ResourceSelection:
    """Deterministically select a profile and cap it to host capacity."""
    host_cpus, host_total, host_avail = host or host_capacity()
    warnings: list[str] = []
    reasons: list[str] = []

    if requested and requested.lower() in PROFILES:
        base = PROFILES[requested.lower()]
        reasons.append(f"requested profile: {base.name}")
    else:
        heavy = bool(set(report.languages) & HEAVY_LANGUAGES)
        if heavy:
            base = LARGE if len(report.test_frameworks) > 2 else MEDIUM
            reasons.append(f"heavy ecosystem {sorted(set(report.languages) & HEAVY_LANGUAGES)}")
        elif report.package_managers:
            base = MEDIUM
            reasons.append("package-manager project")
        else:
            base = SMALL
            reasons.append("lightweight project")

    cpus = min(base.cpus, max(1.0, host_cpus - 0.0))
    # Reserve ~15% of host RAM for the orchestrator/OS.
    mem_cap = int(host_total * 0.85)
    mem = min(base.memory_bytes, mem_cap)
    if cpus < base.cpus:
        warnings.append(f"cpu capped {base.cpus} -> {cpus} by host ({host_cpus} cpus)")
    if mem < base.memory_bytes:
        warnings.append(
            f"memory capped {base.memory} -> {mem // 1024**2}m by host "
            f"({host_total // 1024**2}m total)"
        )

    min_mem = 4 * 1024**3 if (set(report.languages) & HEAVY_LANGUAGES) else 2 * 1024**3
    compatible = cpus >= 1.0 and mem >= min_mem
    if not compatible:
        reasons.append(
            f"insufficient resources: need >= {min_mem // 1024**2}m for "
            f"{sorted(set(report.languages) & HEAVY_LANGUAGES) or 'project'}, "
            f"host allows {mem // 1024**2}m"
        )
    profile = ResourceProfile(base.name, cpus, f"{max(1, mem // 1024**2)}m")
    return ResourceSelection(profile=profile, compatible=compatible, warnings=warnings, reasons=reasons)
