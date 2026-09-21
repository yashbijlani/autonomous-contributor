"""Integration: restart/resume — state survives reload from DB."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contributor.models.state import JobState, JobStatus
from contributor.persistence.database import Database
from contributor.persistence.repositories import JobRepository


def test_resume_after_restart(tmp_path: Path):
    db_path = tmp_path / "c.db"
    db = Database(f"sqlite:///{db_path}")
    repo = JobRepository(db)
    st = JobState(job_id="resume1", repository="o/r", issue_number=3)
    st.current_state = JobStatus.TEST
    st.implementation_attempt = 1
    st.add_event("TEST_STARTED", "x")
    repo.save_incremental(st)
    # simulate process restart: new Database handle
    db2 = Database(f"sqlite:///{db_path}")
    repo2 = JobRepository(db2)
    loaded = repo2.get("resume1")
    assert loaded is not None
    assert loaded.current_state == JobStatus.TEST
    assert loaded.implementation_attempt == 1
    assert loaded.event_history
