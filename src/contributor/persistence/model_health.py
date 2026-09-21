"""Model health cache: avoid re-running inference preflight for every job.

Backed by SQLite (portable schema). Entries expire after MODEL_HEALTH_TTL_S.
A model that fails during a real job is invalidated.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from contributor.opencode.client import ModelSpec
from contributor.persistence.database import Database


@dataclass
class ModelHealth:
    provider: str
    model: str
    variant: str
    status: str  # "ok" | "unavailable"
    latency_s: float
    error_class: str
    last_checked: float

    @property
    def key(self) -> str:
        return f"{self.model}::{self.variant}"

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


class ModelHealthStore:
    def __init__(self, db: Database, ttl_s: int = 3600):
        self.db = db
        self.ttl_s = ttl_s

    def get(self, spec: ModelSpec, *, allow_stale: bool = False) -> ModelHealth | None:
        rows = self.db.execute(
            "SELECT provider, model, variant, status, latency_s, error_class, last_checked "
            "FROM model_health WHERE model_key = ?",
            (spec.key(),),
        )
        if not rows:
            return None
        r = rows[0]
        health = ModelHealth(
            provider=r["provider"], model=r["model"], variant=r["variant"],
            status=r["status"], latency_s=r["latency_s"], error_class=r["error_class"],
            last_checked=r["last_checked"],
        )
        if not allow_stale and (time.time() - health.last_checked) > self.ttl_s:
            return None
        return health

    def put(self, spec: ModelSpec, *, ok: bool, latency_s: float = 0.0, error_class: str = "") -> ModelHealth:
        health = ModelHealth(
            provider=spec.provider, model=spec.model, variant=spec.variant,
            status="ok" if ok else "unavailable", latency_s=latency_s,
            error_class=error_class, last_checked=time.time(),
        )
        self.db.execute_write(
            """INSERT INTO model_health (model_key, provider, model, variant, status, latency_s, error_class, last_checked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_key) DO UPDATE SET
                 provider=excluded.provider, model=excluded.model, variant=excluded.variant,
                 status=excluded.status, latency_s=excluded.latency_s,
                 error_class=excluded.error_class, last_checked=excluded.last_checked""",
            (health.key, health.provider, health.model, health.variant,
             health.status, health.latency_s, health.error_class, health.last_checked),
        )
        return health

    def invalidate(self, spec: ModelSpec) -> None:
        self.db.execute_write("DELETE FROM model_health WHERE model_key = ?", (spec.key(),))

    def all(self) -> list[ModelHealth]:
        rows = self.db.execute(
            "SELECT provider, model, variant, status, latency_s, error_class, last_checked FROM model_health"
        )
        return [
            ModelHealth(provider=r["provider"], model=r["model"], variant=r["variant"],
                        status=r["status"], latency_s=r["latency_s"], error_class=r["error_class"],
                        last_checked=r["last_checked"])
            for r in rows
        ]
