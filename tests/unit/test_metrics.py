"""Unit: PR/CI lifecycle metrics (deterministic, from persisted facts)."""
from contributor.models.state import (
    CIFailureClass,
    CIResult,
    JobState,
    JobStatus,
)
from contributor.observability import events as E
from contributor.observability.metrics import aggregate_metrics, job_metrics


def _job(**kw) -> JobState:
    st = JobState(job_id=kw.pop("job_id", "m1"), repository="o/r", issue_number=1, **kw)
    return st


def test_job_metrics_happy_path():
    st = _job()
    st.execution_mode = "live"
    st.add_event(E.JOB_CREATED)
    st.add_event(E.PUSHED)
    st.add_event(E.PR_OPENED)
    st.add_event(E.CI_PASSED)
    st.pull_request_url = "https://github.com/o/r/pull/1"
    st.pull_request_number = 1
    st.ci_result = CIResult(state="pass")
    st.merge_ready = True
    st.current_state = JobStatus.MERGE_READY
    m = job_metrics(st, usage=[{"duration_s": 2.5}, {"duration_s": 1.5}])
    assert m["pr_created"] and m["ci_state"] == "pass" and m["merge_ready"]
    assert m["push_count"] == 1
    assert m["model_calls"] == 2
    assert m["time_issue_to_pr_s"] is not None


def test_job_metrics_repair_success():
    st = _job(job_id="m2")
    st.ci_repair_attempt = 2
    st.ci_result = CIResult(state="pass")
    st.ci_failure_class = CIFailureClass.TEST_FAILURE
    st.add_event(E.REPAIR_PUSHED)
    m = job_metrics(st)
    assert m["ci_repair_attempts"] == 2
    assert m["ci_repair_succeeded"]


def test_aggregate_rates():
    good = job_metrics(_passing_job("g"))
    repaired = job_metrics(_repaired_job("r"))
    failed = job_metrics(_failing_job("f"))
    agg = aggregate_metrics([good, repaired, failed])
    assert agg["jobs"] == 3
    assert agg["pr_created"] == 3
    assert agg["pr_creation_success_rate"] == 1.0
    assert agg["ci_passed"] == 2
    assert agg["ci_failed"] == 1
    assert agg["ci_pass_rate"] == round(2 / 3, 4)
    assert agg["ci_repair_rate"] == round(1 / 3, 4)
    assert agg["ci_repair_success_rate"] == 1.0


def test_aggregate_empty():
    agg = aggregate_metrics([])
    assert agg["jobs"] == 0
    assert agg["ci_pass_rate"] == 0.0


def _passing_job(job_id):
    st = JobState(job_id=job_id, repository="o/r", issue_number=1)
    st.pull_request_url = "u"
    st.ci_result = CIResult(state="pass")
    st.merge_ready = True
    return st


def _repaired_job(job_id):
    st = _passing_job(job_id)
    st.ci_repair_attempt = 1
    return st


def _failing_job(job_id):
    st = JobState(job_id=job_id, repository="o/r", issue_number=1)
    st.pull_request_url = "u"
    st.ci_result = CIResult(state="fail")
    st.ci_failure_class = CIFailureClass.CODE_FAILURE
    return st
