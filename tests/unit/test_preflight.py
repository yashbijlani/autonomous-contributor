"""Unit: preflight gates expensive jobs; routing separates env-incompatible."""
from contributor.config import Settings
from contributor.graph.routing import after_test
from contributor.opencode.preflight import (
    PREFLIGHT_TIERS,
    any_usable,
    run_all_preflights,
    run_preflight,
)
from contributor.opencode.runner import OpenCodeResult
from contributor.persistence.database import Database
from contributor.persistence.model_health import ModelHealthStore


class _OKRunner:
    def run_with_prompt(self, prompt, *, workdir, model="", variant="", timeout=None):
        assert "/" in model, "preflight must use a full provider/model ID"
        return OpenCodeResult(0, "OK", "", 1.0, model=model, variant=variant)


class _FailRunner:
    def run_with_prompt(self, prompt, *, workdir, model="", variant="", timeout=None):
        return OpenCodeResult(1, "", "Unexpected server error", 1.0, model=model,
                              variant=variant, error_kind="PROVIDER_SERVER_ERROR")


def _settings(**kw):
    base = dict(
        opencode_binary="true",  # exists; version probe may fail -> monkeypatched below
        opencode_model_standard="p/m",
        opencode_model_xhigh="p/m2",
    )
    base.update(kw)
    return Settings(**base)


def test_preflight_ok_with_probe(monkeypatch):
    import contributor.opencode.preflight as pf

    monkeypatch.setattr(pf, "check_binary", lambda s: (True, "OK"))
    monkeypatch.setattr(pf, "check_model_listed", lambda s, spec: (True, "OK"))
    r = run_preflight(_settings(), _OKRunner())
    assert r.ok and r.checks["inference"].startswith("OK")


def test_preflight_blocks_on_inference_failure(monkeypatch):
    import contributor.opencode.preflight as pf

    monkeypatch.setattr(pf, "check_binary", lambda s: (True, "OK"))
    monkeypatch.setattr(pf, "check_model_listed", lambda s, spec: (True, "OK"))
    r = run_preflight(_settings(), _FailRunner())
    assert not r.ok and "inference" in r.checks


def test_preflight_blocks_on_missing_binary():
    r = run_preflight(Settings(opencode_binary="definitely-not-a-binary-xyz"), _OKRunner())
    assert not r.ok and "binary" in r.checks


def test_after_test_env_unsupported():
    s = Settings()
    st = {"test_results": [{"command": "(no test command)", "passed": False}],
          "implementation_attempt": 0, "debug_attempt": 0}
    assert after_test(st, s) == "env_unsupported"


def test_after_test_real_failure_still_debug():
    s = Settings()
    st = {"test_results": [{"command": "bash test/all", "passed": False}],
          "implementation_attempt": 0, "debug_attempt": 0}
    assert after_test(st, s) == "debug"


def test_run_all_preflights_reports_each_tier(monkeypatch):
    import contributor.opencode.preflight as pf

    monkeypatch.setattr(pf, "check_binary", lambda s: (True, "OK"))
    monkeypatch.setattr(pf, "check_model_listed", lambda s, spec: (True, "OK"))
    results = run_all_preflights(_settings(), _OKRunner())
    assert set(results) == set(PREFLIGHT_TIERS)
    assert any_usable(results)


def test_preflight_marks_unavailable_when_probe_fails(monkeypatch):
    import contributor.opencode.preflight as pf

    monkeypatch.setattr(pf, "check_binary", lambda s: (True, "OK"))
    monkeypatch.setattr(pf, "check_model_listed", lambda s, spec: (True, "OK"))
    store = ModelHealthStore(Database("sqlite:///:memory:"), ttl_s=3600)
    r = run_preflight(_settings(), _FailRunner(), tier="strong", health_store=store)
    assert not r.ok
    from contributor.opencode.client import resolve_spec

    assert not store.get(resolve_spec(_settings(), "strong")).is_ok


def test_preflight_reuses_healthy_cache(monkeypatch):
    import contributor.opencode.preflight as pf

    monkeypatch.setattr(pf, "check_binary", lambda s: (True, "OK"))
    monkeypatch.setattr(pf, "check_model_listed", lambda s, spec: (True, "OK"))
    store = ModelHealthStore(Database("sqlite:///:memory:"), ttl_s=3600)

    class _CountingRunner:
        def __init__(self):
            self.calls = 0

        def run_with_prompt(self, prompt, *, workdir, model="", variant="", timeout=None):
            self.calls += 1
            return OpenCodeResult(0, "OK", "", 1.0, model=model, variant=variant)

    runner = _CountingRunner()
    r1 = run_preflight(_settings(), runner, tier="strong", health_store=store)
    r2 = run_preflight(_settings(), runner, tier="strong", health_store=store)
    assert r1.ok and r2.ok and r2.reused
    assert runner.calls == 1  # second call reused the cache
