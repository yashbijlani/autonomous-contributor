"""Unit: triage safety gates."""
from contributor.agents.triage import triage_issue


def test_blocks_secret_exposure():
    tr = triage_issue("leak", "please commit the AWS_SECRET key exposure here " + "x" * 50)
    assert tr.decision.value == "reject"
    assert tr.security_flag


def test_blocks_destructive():
    tr = triage_issue("cleanup", "run rm -rf / on production servers please " + "y" * 60)
    assert tr.decision.value == "reject"


def test_human_required_on_product_decision():
    tr = triage_issue("redesign?", "We need product decision on which approach should we take. " + "z" * 60)
    assert tr.decision.value == "human_required"
    assert tr.requires_human


def test_rejects_empty_body():
    tr = triage_issue("bug", "too short")
    assert tr.decision.value == "reject"


def test_accepts_good_bug():
    tr = triage_issue("Crash on startup", "Traceback (most recent call last): ... app crashes on startup when config missing. " * 3)
    assert tr.decision.value == "accept"
    assert tr.recommended_model in ("high", "xhigh")


def test_likely_files_ignores_absolute_and_cache_paths():
    body = (
        "Traceback shows /home/user/.cache/uv/builds-v0/.tmp/bin/x and "
        "src/uv/resolver.py and tests/test_resolver.py. " * 3
    )
    tr = triage_issue("Resolver crash", body)
    assert "src/uv/resolver.py" in tr.likely_files
    assert "tests/test_resolver.py" in tr.likely_files
    assert not any(p.startswith("/") for p in tr.likely_files)
    assert not any(".cache" in p for p in tr.likely_files)
