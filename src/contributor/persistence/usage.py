"""Model usage accounting (best-effort tokens; always duration/outcome)."""
from __future__ import annotations

from contributor.models.state import utcnow_iso
from contributor.persistence.database import Database


def record_usage(
    db: Database,
    *,
    job_id: str,
    task: str,
    provider: str,
    model: str,
    variant: str,
    tier: str,
    duration_s: float,
    attempt: int = 0,
    outcome: str = "",
    est_tokens: int = 0,
) -> None:
    try:
        db.execute_write(
            """INSERT INTO model_usage (job_id, task, provider, model, variant, tier, duration_s, attempt, outcome, est_tokens, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, task, provider, model, variant, tier, duration_s, attempt, outcome, est_tokens, utcnow_iso()),
        )
    except Exception:
        # Accounting must never break a job.
        pass


def list_usage(db: Database, job_id: str | None = None) -> list[dict]:
    if job_id:
        rows = db.execute("SELECT * FROM model_usage WHERE job_id = ? ORDER BY id", (job_id,))
    else:
        rows = db.execute("SELECT * FROM model_usage ORDER BY id DESC LIMIT 200")
    return [dict(r) for r in rows]


def summarize_usage(db: Database) -> dict:
    rows = db.execute(
        "SELECT tier, COUNT(*) AS calls, SUM(duration_s) AS seconds FROM model_usage GROUP BY tier"
    )
    return {r["tier"] or "unknown": {"calls": r["calls"], "seconds": round(r["seconds"] or 0, 1)} for r in rows}
