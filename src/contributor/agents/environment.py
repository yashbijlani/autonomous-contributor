"""Environment Discovery agent: read-only inspection of a repository to decide
how it must be built/tested and whether the sandbox can run it.

Deterministic by design (facts, not LLM guesses). Never modifies the repo.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from contributor.models.state import EnvironmentReport, EnvironmentStrategy
from contributor.sandbox.strategy import choose_strategy

# Extension -> language
EXT_LANG = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".sh": "shell", ".bash": "shell",
    ".lua": "lua", ".cs": "csharp", ".swift": "swift",
}

# Known host tools we care about (missing in slim containers).
KNOWN_TOOLS = [
    "lua", "luajit", "magick", "convert", "jq", "mise", "updatedb", "systemctl",
    "docker", "podman", "hyprctl", "wpctl", "nmcli", "pactl", "gsettings",
    "dbus-send", "udevadm", "mount", "chroot", "pacman", "sudo", "ffmpeg",
    "pandoc", "chromium", "google-chrome", "xdotool", "wl-copy", "gum",
]

# Files worth fetching for analysis (bounded).
CANDIDATE_FILES = [
    "README.md", "README", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md",
    "Makefile", "makefile", "GNUmakefile",
    "test/all", "test/run", "run-tests.sh", "scripts/test.sh",
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Cargo.toml", "go.mod", "Gemfile", "composer.json", "pom.xml", "build.gradle",
    "CMakeLists.txt", "Dockerfile", ".devcontainer/devcontainer.json",
    ".github/workflows/ci.yml", ".github/workflows/test.yml", ".github/workflows/ci.yaml",
    ".github/workflows/tests.yml",
]


def _langs_from_paths(paths: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    for p in paths:
        ext = Path(p).suffix.lower()
        lang = EXT_LANG.get(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return [l for l, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def _manifests(paths: set[str]) -> tuple[list[str], list[str], list[str]]:
    pms: list[str] = []
    builds: list[str] = []
    frameworks: list[str] = []

    def has(name: str) -> bool:
        return name in paths or any(p.endswith("/" + name) for p in paths)

    if has("package.json"):
        pms.append("npm")
        frameworks.append("node-test")
    if has("pnpm-lock.yaml"):
        pms.append("pnpm")
    if has("yarn.lock"):
        pms.append("yarn")
    if has("pyproject.toml") or has("setup.py") or has("requirements.txt"):
        pms.append("pip")
    if has("uv.lock"):
        pms.append("uv")
    if has("poetry.lock"):
        pms.append("poetry")
    if has("Cargo.toml"):
        pms.append("cargo")
        builds.append("cargo")
        frameworks.append("cargo-test")
    if has("go.mod"):
        pms.append("go")
        frameworks.append("go-test")
    if has("Gemfile"):
        pms.append("bundler")
        frameworks.append("rspec")
    if has("composer.json"):
        pms.append("composer")
        frameworks.append("phpunit")
    if has("pom.xml"):
        builds.append("maven")
    if has("build.gradle") or has("build.gradle.kts"):
        builds.append("gradle")
    if has("CMakeLists.txt"):
        builds.append("cmake")
    if has("Makefile"):
        builds.append("make")
    if has("test/all") or any(p.startswith("test/") and p.endswith(".bats") for p in paths):
        frameworks.append("shell-test")
    return pms, builds, frameworks


def _detect_tools(text: str) -> list[str]:
    found: list[str] = []
    low = text.lower()
    for tool in KNOWN_TOOLS:
        if re.search(rf"(^|[\s\"'/$(`]){re.escape(tool)}(\b|$)", low):
            found.append(tool)
    return found


def _test_commands(paths: set[str], frameworks: list[str], ci_text: str) -> list[str]:
    cmds: list[str] = []
    if "test/all" in paths:
        cmds.append("bash test/all")
    if "shell-test" in frameworks and "bash test/all" not in cmds:
        cmds.append("bats test/")
    if "pyproject.toml" in paths or "setup.py" in paths:
        cmds.append("python -m pytest -q")
    if "package.json" in paths:
        cmds.append("npm test --silent")
    if "cargo-test" in frameworks:
        cmds.append("cargo test --quiet")
    if "go-test" in frameworks:
        cmds.append("go test ./...")
    if "rspec" in frameworks:
        cmds.append("bundle exec rspec")
    if "phpunit" in frameworks:
        cmds.append("vendor/bin/phpunit")
    # CI run lines (best-effort, allowlisted later)
    for m in re.findall(r"run:\s*([^\n]+)", ci_text):
        c = m.strip().strip("'\"")
        if c and c not in cmds:
            cmds.append(c)
    return cmds[:6]


def discover_environment(
    *,
    paths: list[str],
    files: dict[str, str],
    issue_body: str = "",
) -> EnvironmentReport:
    """Analyse repo paths + selected file contents. Deterministic."""
    pathset = set(paths)
    signals: list[str] = []
    reasons: list[str] = []

    languages = _langs_from_paths(paths)
    pms, builds, frameworks = _manifests(pathset)

    combined = "\n".join(files.values()) + "\n" + (issue_body or "")
    # CI/test/install scripts carry the strongest environment signals.
    env_text = "\n".join(
        v for k, v in files.items()
        if any(s in k for s in ("Makefile", "test/", ".github/", "install", "Dockerfile", "devcontainer", "ci."))
    ) or combined

    required_tools = sorted(set(_detect_tools(env_text) + _detect_tools(issue_body)))
    test_commands = _test_commands(pathset, frameworks, files.get(".github/workflows/ci.yml", "") + files.get(".github/workflows/test.yml", ""))

    if any(p.startswith(".github/workflows/") or "/workflows/" in p for p in pathset):
        signals.append("github_actions")
    if "Dockerfile" in pathset or any(p.endswith("/Dockerfile") for p in pathset):
        signals.append("dockerfile")
    if ".devcontainer/devcontainer.json" in pathset:
        signals.append("devcontainer")
    if "Makefile" in pathset:
        signals.append("makefile")
    if "test/all" in pathset:
        signals.append("shell_test_suite")

    blob = (combined + "\n" + env_text).lower()
    requires_systemd = bool(re.search(r"\bsystemctl\b|\bsystemd\b|\.service\b", blob))
    privileged = bool(re.search(r"\bsudo\b|\bchroot\b|\bpacman\b|\bmount\b|\budevadm\b", blob))
    unsafe = bool(re.search(r"\bchroot\b|\bmount\b|\bmkfs\b|\bdd\s+if=|\budevadm\b|\blosetup\b|\bmodprobe\b", blob))
    network_required = bool(re.search(r"\bcurl\b|\bwget\b|\bnpm install\b|\bpip install\b|\bgit clone\b", blob))
    if requires_systemd:
        signals.append("systemd")
    if privileged:
        signals.append("privileged")
    if unsafe:
        signals.append("unsafe_ops")

    # Host-runtime heuristic: shell install suites / many missing host tools /
    # systemd / privileged operations / hardware configs.
    host_markers = sum([
        requires_systemd,
        privileged,
        "shell_test_suite" in signals,
        len(required_tools) >= 3,
        "install/" in " ".join(paths) or any(p.startswith("install/") for p in paths),
        bool(re.search(r"\bhyprland\b|\bwaybar\b|\bomarchy\b", blob)),
    ])
    requires_host_runtime = host_markers >= 3

    if "shell_test_suite" in signals and required_tools:
        reasons.append(f"shell test suite references host tools: {', '.join(required_tools[:6])}")
    if requires_systemd:
        reasons.append("uses systemd/systemctl (unavailable in slim sandbox)")
    if privileged:
        reasons.append("uses privileged operations (sudo/pacman/mount/udev)")

    container_compatible = not (requires_systemd or requires_host_runtime or privileged)
    if container_compatible:
        reasons.append("no host-runtime or privileged requirements detected")

    evidence = len(paths) > 0
    confidence = 0.3
    if languages:
        confidence += 0.15
    if pms or frameworks:
        confidence += 0.2
    if files:
        confidence += 0.15
    if required_tools:
        confidence += 0.15
    confidence = round(min(0.95, confidence), 2)
    if not evidence:
        confidence = 0.2

    report = EnvironmentReport(
        languages=languages,
        package_managers=sorted(set(pms)),
        build_systems=sorted(set(builds)),
        test_frameworks=sorted(set(frameworks)),
        required_tools=required_tools,
        test_commands=test_commands,
        requires_systemd=requires_systemd,
        requires_host_runtime=requires_host_runtime,
        privileged_ops=privileged,
        unsafe_ops=unsafe,
        network_required=network_required,
        container_compatible=container_compatible,
        confidence=confidence,
        signals=sorted(set(signals)),
        reasons=reasons,
    )
    report.strategy = choose_strategy(report)
    return report


def discover_environment_from_github(client, owner: str, repo: str, ref: str, issue_body: str = "") -> EnvironmentReport:
    """Fetch the repo tree + key files from GitHub, then analyse."""
    tree = []
    try:
        tree = client.get_tree(owner, repo, ref)
    except Exception:
        tree = []
    paths = [t.get("path", "") for t in tree if t.get("type") == "blob"]
    files: dict[str, str] = {}
    for cand in CANDIDATE_FILES:
        if cand in paths or not paths:
            try:
                content = client.get_file(owner, repo, cand, ref=ref)
            except Exception:
                content = None
            if content:
                files[cand] = content[:20000]
    return discover_environment(paths=paths, files=files, issue_body=issue_body)
