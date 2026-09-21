"""Integration: restart/resume preserves job state, model health and usage."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contributor.models.state import EnvironmentReport, JobState, JobStatus
from contributor.opencode.client import ModelSpec
from contributor.persistence.database import Database
from contributor.persistence.model_health import ModelHealthStore
from contributor.persistence.repositories import JobRepository
from contributor.persistence.usage import list_usage, record_usage, summarize_usage


def test_resume_preserves_environment_report(tmp_path: Path):
    db_path = tmp_path / "c.db"
    repo = JobRepository(Database(f"sqlite:///{db_path}"))
    st = JobState(job_id="r2", repository="o/r", issue_number=9)
    st.current_state = JobStatus.IMPLEMENT
    st.environment_report = EnvironmentReport(languages=["shell"], required_tools=["lua"], container_compatible=False)
    repo.save_incremental(st)

    # Simulate process restart.
    loaded = JobRepository(Database(f"sqlite:///{db_path}")).get("r2")
    assert loaded is not None
    assert loaded.environment_report is not None
    assert loaded.environment_report.required_tools == ["lua"]
    assert loaded.current_state == JobStatus.IMPLEMENT


def test_model_health_persists_across_restart(tmp_path: Path):
    db_path = tmp_path / "h.db"
    spec = ModelSpec(provider="p", model="p/m", variant="high")
    ModelHealthStore(Database(f"sqlite:///{db_path}"), ttl_s=3600).put(spec, ok=True, latency_s=3.2)
    store2 = ModelHealthStore(Database(f"sqlite:///{db_path}"), ttl_s=3600)
    got = store2.get(spec)
    assert got is not None and got.is_ok and got.latency_s == 3.2


def test_usage_persists_across_restart(tmp_path: Path):
    db_path = tmp_path / "u.db"
    db = Database(f"sqlite:///{db_path}")
    record_usage(db, job_id="j1", task="implementation", provider="p", model="p/m",
                 variant="high", tier="strong", duration_s=12.5, attempt=1, outcome="ok")
    db2 = Database(f"sqlite:///{db_path}")
    rows = list_usage(db2, "j1")
    assert len(rows) == 1
    assert rows[0]["tier"] == "strong"
    assert summarize_usage(db2)["strong"]["calls"] == 1
