"""Repository layer: jobs + events. Single place that persists JobState."""
from __future__ import annotations

import json

from contributor.models.state import JobEvent, JobState, JobStatus
from contributor.persistence.database import Database, with_retries


class JobRepository:
    def __init__(self, db: Database):
        self.db = db

    def save(self, state: JobState) -> None:
        """Legacy alias — delegates to save_incremental to avoid duplicate events."""
        self.save_incremental(state)

    def save_incremental(self, state: JobState) -> None:
        """Save job row + only new events since last save."""
        state.touch()
        payload = state.model_dump_json()
        persisted = int(getattr(state, "_persisted_events", 0))

        def _op():
            self.db.execute_write(
                """INSERT INTO jobs (job_id, repository, issue_number, current_state,
                   escalated, done, pull_request_url, created_at, updated_at, state_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     repository=excluded.repository,
                     issue_number=excluded.issue_number,
                     current_state=excluded.current_state,
                     escalated=excluded.escalated,
                     done=excluded.done,
                     pull_request_url=excluded.pull_request_url,
                     created_at=excluded.created_at,
                     updated_at=excluded.updated_at,
                     state_json=excluded.state_json""",
                (
                    state.job_id,
                    state.repository,
                    state.issue_number,
                    state.current_state.value,
                    int(state.escalated),
                    int(state.done),
                    state.pull_request_url,
                    state.created_at,
                    state.updated_at,
                    payload,
                ),
            )
            for ev in state.event_history[persisted:]:
                self.db.execute_write(
                    """INSERT INTO events (job_id, type, at, message, agent, model, attempt, data_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        state.job_id,
                        ev.type,
                        ev.at,
                        ev.message,
                        ev.agent,
                        ev.model,
                        ev.attempt,
                        json.dumps(ev.data),
                    ),
                )

        with_retries(_op)
        try:
            state._persisted_events = len(state.event_history)  # type: ignore[attr-defined]
        except Exception:
            pass

    def get(self, job_id: str) -> JobState | None:
        rows = self.db.execute("SELECT state_json FROM jobs WHERE job_id = ?", (job_id,))
        if not rows:
            return None
        state = JobState.model_validate_json(rows[0]["state_json"])
        try:
            state._persisted_events = len(state.event_history)  # type: ignore[attr-defined]
        except Exception:
            pass
        return state

    def list(self, limit: int = 50) -> list[JobState]:
        rows = self.db.execute(
            "SELECT state_json FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        out = []
        for r in rows:
            try:
                out.append(JobState.model_validate_json(r["state_json"]))
            except Exception:
                continue
        return out

    def events(self, job_id: str) -> list[JobEvent]:
        rows = self.db.execute(
            "SELECT type, at, message, agent, model, attempt, data_json FROM events WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
        out = []
        for r in rows:
            try:
                import json as _json

                out.append(
                    JobEvent(
                        type=r["type"],
                        at=r["at"],
                        message=r["message"],
                        agent=r["agent"],
                        model=r["model"],
                        attempt=r["attempt"],
                        data=_json.loads(r["data_json"] or "{}"),
                    )
                )
            except Exception:
                continue
        return out
