"""Unit: cheap-model triage parsing + fallback behavior."""
from contributor.benchmark.llm_triage import llm_assess
from contributor.config import Settings


class _Result:
    def __init__(self, exit_code=0, stdout="", stderr="", error_kind=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error_kind = error_kind


class _Runner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run_with_prompt(self, prompt, *, workdir, model="", variant="", timeout=None):
        self.calls.append({"model": model, "variant": variant, "prompt": prompt})
        return self.result


_JSON = """{
  "actionable": true,
  "reproducible": true,
  "clear_expected_behavior": true,
  "likely_code_change": true,
  "likely_test_change": true,
  "container_testable": true,
  "requires_maintainer_decision": false,
  "requires_external_service": false,
  "estimated_complexity": 2,
  "confidence": 0.8,
  "decision": "accept",
  "reason": "concrete bug with reproduction"
}"""


def test_llm_assess_parses_json():
    runner = _Runner(_Result(stdout=_JSON))
    res = llm_assess(Settings(), runner, title="Crash", body="steps...", labels=["bug"], workdir=".")
    assert res.ok
    assert res.decision == "accept"
    assert res.assessment.actionable
    assert res.assessment.estimated_complexity == 2
    assert res.assessment.confidence == 0.8
    assert "llm_triage" in res.assessment.signals


def test_llm_assess_tolerates_prose_around_json():
    runner = _Runner(_Result(stdout=f"Here is the classification:\n{_JSON}\nDone."))
    res = llm_assess(Settings(), runner, title="Crash", body="steps...", workdir=".")
    assert res.ok
    assert res.decision == "accept"


def test_llm_assess_falls_back_on_unparseable_output():
    runner = _Runner(_Result(stdout="not json at all"))
    res = llm_assess(
        Settings(), runner, title="Crash", body="steps to reproduce: run it", workdir="."
    )
    assert not res.ok
    assert res.error == "unparseable_json"
    # deterministic fallback assessment is still returned
    assert res.assessment is not None


def test_llm_assess_falls_back_on_provider_error():
    runner = _Runner(_Result(exit_code=1, error_kind="PROVIDER_SERVER_ERROR"))
    res = llm_assess(Settings(), runner, title="Crash", body="body", workdir=".")
    assert not res.ok
    assert res.error == "PROVIDER_SERVER_ERROR"


def test_llm_assess_rejects_unknown_decision():
    runner = _Runner(_Result(stdout=_JSON.replace('"accept"', '"maybe"')))
    res = llm_assess(Settings(), runner, title="Crash", body="body", workdir=".")
    assert res.ok
    assert res.decision == "reject"
