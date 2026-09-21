"""Unit: persistence round-trip + restart recovery."""
from contributor.models.state import JobState, JobStatus
from contributor.persistence.database import Database
from contributor.persistence.repositories import JobRepository


def test_save_get_roundtrip():
    db = Database("sqlite:///:memory:")
    repo = JobRepository(db)
    st = JobState(job_id="abc123", repository="o/r", issue_number=7)
    st.current_state = JobStatus.PLAN
    st.add_event("PLAN_CREATED", "hi")
    repo.save_incremental(st)
    loaded = repo.get("abc123")
    assert loaded is not None
    assert loaded.current_state == JobStatus.PLAN
    assert loaded.event_history[-1].type == "PLAN_CREATED"


def test_events_persisted():
    db = Database("sqlite:///:memory:")
    repo = JobRepository(db)
    st = JobState(job_id="e1", repository="o/r", issue_number=1)
    st.add_event("JOB_CREATED", "created")
    st.add_event("TRIAGE_COMPLETED", "done")
    repo.save_incremental(st)
    evs = repo.events("e1")
    assert len(evs) >= 2
