"""Central configuration. All tunables come from env vars (see .env.example)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # GitHub
    github_token: str = Field(default="", description="GitHub PAT (repo scope)")
    github_api_base: str = Field(default="https://api.github.com")
    github_timeout_s: float = 30.0

    # OpenCode — model selection uses provider/model IDs; reasoning effort is a
    # separate --variant flag. Tier aliases (fast/cheap/strong/max/high/xhigh)
    # are NEVER valid --model values.
    opencode_binary: str = Field(default="opencode")
    opencode_default_model: str = Field(default="")  # empty => opencode default

    # Back-compat tier aliases ("high"/"xhigh").
    opencode_model_standard: str = Field(
        default="opencode-go/deepseek-v4.1-flash",
        description="Full provider/model ID for the legacy 'high' tier",
    )
    opencode_model_xhigh: str = Field(
        default="opencode-go/deepseek-v4.1-flash",
        description="Full provider/model ID for the legacy 'xhigh' tier",
    )
    opencode_variant_standard: str = Field(default="high", description="--variant for legacy 'high'")
    opencode_variant_xhigh: str = Field(default="xhigh", description="--variant for legacy 'xhigh'")

    # Task-complexity tiers (see ModelRouter). Empty model falls back to the
    # standard/xhigh model so configuration stays optional.
    opencode_model_fast: str = Field(default="opencode-go/deepseek-v4.1-flash")
    opencode_variant_fast: str = Field(default="")
    opencode_model_cheap: str = Field(default="opencode-go/deepseek-v4.1-flash")
    opencode_variant_cheap: str = Field(default="")
    opencode_model_strong: str = Field(default="opencode-go/deepseek-v4.1-flash")
    opencode_variant_strong: str = Field(default="")
    opencode_model_max: str = Field(default="opencode-go/deepseek-v4.1-flash")
    opencode_variant_max: str = Field(default="")

    # Optional JSON policy override for ModelRouter (task_type -> tier).
    model_router_policy: str = Field(default="")
    # Model health cache TTL (seconds). Preflight results are reused within TTL.
    model_health_ttl_s: int = 3600
    # Per-model inference probe timeout (seconds). Slow provider => mark
    # unavailable rather than blocking the whole run.
    model_preflight_timeout_s: int = 90

    opencode_min_version: str = Field(default="1.18.0", description="Minimum supported opencode version")
    opencode_timeout_s: int = 1800
    opencode_extra_args: str = Field(default="")

    # Limits (hard caps — never infinite loops)
    max_implementation_attempts: int = 3
    max_debug_attempts: int = 3
    max_review_cycles: int = 3
    max_ci_repair_cycles: int = 2
    max_concurrent_jobs: int = 2
    job_timeout_s: int = 7200
    command_timeout_s: int = 600
    test_timeout_s: int = 900

    # Environment discovery / strategy
    env_discovery_enabled: bool = True
    # When True, a HOST_REQUIRED/INCOMPATIBLE repo is escalated before any
    # implementation. When False (default) implementation proceeds but env
    # failures are classified without entering the debug loop.
    env_hard_gate: bool = False
    usage_enabled: bool = True

    # Sandbox / Docker
    sandbox_image: str = Field(default="contributor-sandbox:latest")
    sandbox_cpu: str = Field(default="1.0")
    sandbox_memory: str = Field(default="2g")
    sandbox_pids_limit: int = 256
    sandbox_network: str = Field(default="bridge", description="bridge|none|<network name>")
    sandbox_allow_network: bool = True
    workspaces_root: str = Field(default="./.workspaces")
    docker_timeout_s: int = 120
    # When True and docker is unavailable, fall back to a filesystem-isolated local dir
    # (used for tests/CI). Production should set REQUIRE_DOCKER=1.
    sandbox_fallback_local: bool = True
    require_docker: bool = False

    # --- In-sandbox execution (OpenCode + repository env inside one container) ---
    # When True (and Docker is available) OpenCode, dependencies, toolchain and
    # tests all run inside the same long-lived sandbox session.
    sandbox_execution: bool = True
    sandbox_workspace_mount: str = Field(default="/workspace/repo")
    sandbox_env_mount: str = Field(default="/opt/env")
    # Host cache directory for toolchains/dependencies, reused across jobs.
    env_cache_root: str = Field(default="~/.cache/contributor-env")
    # OpenCode material mounted read-only into the sandbox (never copied into images).
    opencode_host_binary: str = Field(default="", description="Host path; resolved via PATH when empty")
    opencode_auth_file: str = Field(default="~/.local/share/opencode/auth.json")
    opencode_config_file: str = Field(default="~/.config/opencode/opencode.json")
    # Resource profile: auto|small|medium|large (capped by host capacity).
    resource_profile: str = Field(default="auto")
    bootstrap_timeout_s: int = 1800
    bootstrap_enabled: bool = True

    # Persistence
    database_url: str = Field(default="sqlite:///./contributor.db")
    # Resolve sqlite path relative to CWD at runtime; postgres URLs passed through.
    # Any URL starting with postgresql:// is accepted for future migration.

    # Autonomous mode
    autonomous_poll_interval_s: int = 300

    # PR
    pr_ai_disclosure: bool = True
    pr_ai_disclosure_text: str = Field(
        default="This PR was created with AI assistance (autonomous-contributor + OpenCode). Human review requested."
    )
    default_branch_protection: tuple[str, ...] = ("main", "master")

    # Observability
    log_level: str = Field(default="INFO")
    log_json: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_workspace_root(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    p = Path(s.workspaces_root).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_db_path(database_url: str) -> Path | None:
    """Return filesystem path for sqlite URLs, else None (postgres etc)."""
    if database_url.startswith("sqlite:///"):
        raw = database_url[len("sqlite:///") :]
        # handle :memory:
        if raw == ":memory:":
            return None
        return Path(raw)
    if database_url.startswith("sqlite://"):
        return Path(database_url[len("sqlite://") :])
    return None
