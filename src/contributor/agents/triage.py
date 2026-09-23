"""Triage agent: deterministic safety + completeness gates, heuristic scoring.

Never auto-works on:
- credential/secret exposure, destructive infra, unclear product decisions,
  breaking API changes without maintainer direction, explicit human-auth requests.

The LLM may refine scoring, but the blocklist below is programmatic and cannot
be overridden by model output.
"""
from __future__ import annotations

import re

from contributor.models.state import TriageAssessment, TriageDecision, TriageResult

# --- hard blocklist (programmatic, not LLM-decidable) ---
BLOCKLIST = [
    r"private key", r"\bpassword\b.*(share|expos|commit|log)", r"aws_secret", r"api[_-]?key.*expos",
    r"\bsecret\b.*(commit|expos|share|plaintext)", r"credentials?\s+(expos|leak|shar|commit)",
    r"rm\s+-rf\s+/", r"drop\s+(table|database)", r"delete\s+production", r"destroy\s+infra",
    r"format\s+c:", r"delete\s+s3\s+bucket",
    r"human\s+(approval|authorization)\s+required", r"requires?\s+human\s+auth",
    r"decrypt", r"bypass\s+auth", r"disable\s+(auth|tls|ssl)\s+in\s+prod",
]

HUMAN_REQUIRED_PATTERNS = [
    r"\bTBD\b", r"needs?\s+discussion", r"which\s+(design|approach)\s+should\s+we",
    r"need\s+(product|design|maintainer)\s+(decision|approval|input)",
    r"product\s+decision", r"design\s+decision", r"breaking\s+change",
    r"breaking\s+api\s+change", r"redesign\s+the\s+api",
    r"major\s+version", r"should\s+we\s+(deprecat|remov|redesign)",
    r"unclear\s+(requirement|spec)", r"ambiguous",
]

ISSUE_TYPE_PATTERNS = {
    "bug": [r"\bbug\b", r"\berror\b", r"\bcrash\b", r"\bexception\b", r"\btraceback\b", r"doesn.?t work", r"broken"],
    "feature": [r"\bfeature\b", r"add support", r"please add", r"enhancement", r"would be nice"],
    "docs": [r"\bdocs?\b", r"documentation", r"readme", r"typo"],
    "security": [r"\bvulnerab", r"\bcve\b", r"\bexploit\b", r"\bxss\b", r"\bsql injection\b"],
    "test": [r"\bflaky test\b", r"test coverage", r"\bpytest\b.*fail"],
    "refactor": [r"\brefactor\b", r"clean.?up", r"tech.?debt"],
}

MIN_BODY_LENGTH = 20


def _matches(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return p
    return None


def triage_issue(
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
    repo_readme: str = "",
    repo_contributing: str = "",
) -> TriageResult:
    labels = labels or []
    text = f"{title}\n{body}"
    # 1. hard blocklist
    hit = _matches(BLOCKLIST, text)
    if hit:
        return TriageResult(
            decision=TriageDecision.REJECT,
            issue_type="security" if re.search(r"secret|credential|key|auth", hit) else "unknown",
            difficulty=5, confidence=0.95, requires_human=False,
            reason=f"Blocked by safety policy (pattern {hit!r}). Requires explicit human handling outside automation.",
            recommended_model="xhigh", security_flag=True,
        )
    # 2. human-required detection
    hhit = _matches(HUMAN_REQUIRED_PATTERNS, text)
    low_labels = {l.lower() for l in labels}
    if hhit or "needs-discussion" in low_labels or "needs-design" in low_labels:
        return TriageResult(
            decision=TriageDecision.HUMAN_REQUIRED,
            difficulty=4, confidence=0.8, requires_human=True,
            reason=f"Requires maintainer/product decision (matched {hhit or 'label'}). Escalating.",
            recommended_model="xhigh",
        )
    # 3. completeness gate
    if len((body or "").strip()) < MIN_BODY_LENGTH:
        return TriageResult(
            decision=TriageDecision.REJECT, difficulty=2, confidence=0.9, requires_human=False,
            reason=f"Issue body too short ({len((body or '').strip())} chars < {MIN_BODY_LENGTH}); not enough information.",
            recommended_model="high",
        )
    # 4. issue type
    issue_type: str = "unknown"
    for kind, pats in ISSUE_TYPE_PATTERNS.items():
        if _matches(pats, text):
            issue_type = kind
            break
    # 5. difficulty heuristic
    difficulty = 2
    if len(text) > 2000:
        difficulty += 1
    if re.search(r"race|deadlock|concurren|distributed|migration|performance|architect", text, re.I):
        difficulty += 1
    if re.search(r"stack ?trace|traceback|core dump", text, re.I):
        difficulty = max(2, difficulty - 0)  # stack trace helps, keep
    if issue_type == "docs":
        difficulty = 1
    elif issue_type == "feature":
        difficulty = max(difficulty, 3)
    difficulty = max(1, min(5, difficulty))
    # 6. likely files: extract repo-relative paths mentioned. Absolute paths and
    # home-dir cache paths (common in issue text) are dropped so the plan is not
    # polluted with non-existent files.
    raw_paths = re.findall(
        r"[A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|rb|php|toml)", text
    )
    likely: list[str] = []
    for p in raw_paths:
        if p.startswith(("/", "~", "$")) or ".." in p:
            continue
        if any(seg.startswith(".") and seg not in (".",) for seg in p.split("/")):
            continue
        if p not in likely:
            likely.append(p)
    likely = sorted(likely)[:10]
    confidence = 0.7 if len((body or "")) > 100 else 0.55
    tier = "xhigh" if difficulty >= 4 else "high"
    return TriageResult(
        decision=TriageDecision.ACCEPT, issue_type=issue_type,  # type: ignore[arg-type]
        difficulty=difficulty, confidence=confidence, likely_files=likely,
        requires_human=False,
        reason=f"Accepted: {issue_type} (difficulty {difficulty}). Sufficient detail to attempt.",
        recommended_model=tier,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Structured solvability assessment (benchmark triage)
#
# Every dimension is factual and derived from the issue body/title/labels.
# Labels inform but never decide: the body is always inspected. There is no
# aggregate score; callers filter on explicit dimensions.
# ---------------------------------------------------------------------------

REPRO_PATTERNS = [
    r"steps? to reproduce", r"to reproduce", r"\breproduc", r"\brepro\b",
    r"minimal (repro|example|reproduction)", r"how to reproduce",
    r"traceback", r"stack ?trace", r"```",
    r"expected\b.{0,80}\b(actual|got|instead|but)\b",
]
EXPECTED_PATTERNS = [
    r"expected (behavior|behaviour|result|output|value)", r"\bexpected\b",
    r"should (be|return|print|raise|work|not)",
    r"i expected", r"acceptance criteri", r"instead(,| )", r"would expect",
]
CODE_CHANGE_PATTERNS = [
    r"\bbug\b", r"\bcrash", r"\berror\b", r"\bexception\b", r"traceback", r"regression",
    r"incorrect", r"\bwrong\b", r"doesn.?t work", r"\bnot work", r"\bfails?\b", r"broken",
]
TEST_CHANGE_PATTERNS = [r"\btest", r"regression", r"coverage", r"fixture", r"\bassert"]
DOCS_ONLY_PATTERNS = [r"documentation", r"\bdocs?\b", r"\btypo\b", r"\breadme\b"]
MAINTAINER_PATTERNS = HUMAN_REQUIRED_PATTERNS + [
    r"\brfc\b", r"proposal", r"design doc", r"which approach", r"should we keep",
]
EXTERNAL_SERVICE_PATTERNS = [
    r"external (service|api|dependency|system)", r"third.?party", r"\bwebhook\b",
    r"\boauth\b", r"\b(s3|aws|gcp|azure|postgres|mysql|redis|kafka|rabbitmq|elasticsearch)\b",
    r"\bkubernetes\b", r"\bk8s\b", r"\bdocker registry\b", r"\bapi key\b",
    r"network (access|required)", r"requires? (a|an) (server|database|service)",
]
HOST_ONLY_PATTERNS = [
    r"\bsystemd\b", r"\bsystemctl\b", r"\bsudo\b", r"\bwindows\b", r"\bmacos?\b",
    r"\bmac os\b", r"\bgpu\b", r"\bcuda\b", r"graphics? card", r"\bdisplay\b",
    r"\bandroid\b", r"\bios\b", r"\bhardware\b", r"\busb\b", r"serial port",
    r"\bbluetooth\b", r"\bcamera\b",
]
SECURITY_PATTERNS = [
    r"\bsecurity\b", r"\bvulnerab", r"\bcve\b", r"\bexploit\b", r"\bxss\b",
    r"sql injection", r"path traversal", r"arbitrary (file|code|command|write|read)",
    r"symlink.*(truncat|overwrit|escape|outside|follow)", r"\btoctou\b",
    r"privilege escalation", r"denial of service", r"remote code execution",
]
COMPLEXITY_HIGH_PATTERNS = [
    r"architect", r"\brefactor", r"\bmigration\b", r"race condition", r"deadlock",
    r"concurren", r"distributed", r"redesign", r"breaking change", r"large",
    r"across (many|multiple) modules", r"new subsystem",
]
SOURCE_PATH_RE = re.compile(
    r"[A-Za-z0-9_./-]+\.(?:py|rs|ts|tsx|js|jsx|go|java|rb|php|c|cpp|h|toml|yaml|yml|json)"
)
CODE_LOCATION_HINTS = [r"\bdef \w+", r"\bclass \w+", r"function \w+", r"module\b", r"\bsrc/\w+"]

AVOID_LABELS = {
    "needs-mre", "needs mre", "needs-repro", "question", "support", "invalid",
    "duplicate", "wontfix", "stale", "security", "breaking-change", "rfc", "discussion",
}
GOOD_LABELS = {"good first issue", "good-first-issue", "help wanted", "help-wanted", "e-easy", "easy"}


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _label_set(labels: list[str]) -> set[str]:
    return {str(l).strip().lower() for l in (labels or [])}


def assess_solvability(
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
) -> TriageAssessment:
    """Deterministic structured assessment. Inspects the body; labels are advisory."""
    labels_norm = _label_set(labels or [])
    text = f"{title}\n{body or ''}"
    low = text.lower()
    signals: list[str] = []

    clear_reproduction = _any(REPRO_PATTERNS, text)
    clear_expected = _any(EXPECTED_PATTERNS, text)
    code_signal = _any(CODE_CHANGE_PATTERNS, text)
    test_signal = _any(TEST_CHANGE_PATTERNS, text)
    docs_only = _any(DOCS_ONLY_PATTERNS, text) and not code_signal
    security = _any(SECURITY_PATTERNS, text) or "security" in labels_norm
    maintainer = security or _any(MAINTAINER_PATTERNS, text) or bool(
        labels_norm & {"needs-discussion", "needs-design", "breaking-change", "rfc", "discussion"}
    )
    external = _any(EXTERNAL_SERVICE_PATTERNS, text)
    host_only = _any(HOST_ONLY_PATTERNS, text)

    mentions_source = bool(SOURCE_PATH_RE.search(text))
    code_location = mentions_source or _any(CODE_LOCATION_HINTS, text)

    if clear_reproduction:
        signals.append("reproduction_steps")
    if clear_expected:
        signals.append("expected_behavior")
    if code_signal:
        signals.append("code_signal")
    if test_signal:
        signals.append("test_signal")
    if docs_only:
        signals.append("docs_only")
    if security:
        signals.append("security")
    if maintainer:
        signals.append("maintainer_decision")
    if external:
        signals.append("external_service")
    if host_only:
        signals.append("host_only")
    if code_location:
        signals.append("code_location")
    if labels_norm & GOOD_LABELS:
        signals.append("good_first_issue_label")
    avoid = labels_norm & AVOID_LABELS
    if avoid:
        signals.append("avoid_label:" + ",".join(sorted(avoid)))

    # Complexity (1..5) from body size, scope keywords, files mentioned, labels.
    complexity = 2
    if len(body or "") > 2500:
        complexity += 1
    if _any(COMPLEXITY_HIGH_PATTERNS, text):
        complexity += 2
    if len(set(SOURCE_PATH_RE.findall(text))) > 3:
        complexity += 1
    if docs_only:
        complexity -= 2
    if labels_norm & GOOD_LABELS:
        complexity = min(complexity, 2)
    complexity = max(1, min(5, complexity))

    body_stripped = (body or "").strip()
    blocked_label = bool(labels_norm & {"needs-mre", "needs mre", "needs-repro"})
    question_label = bool(labels_norm & {"question", "support"})
    actionable = (
        len(body_stripped) >= 40
        and not blocked_label
        and not question_label
        and not docs_only
    )
    if len(body_stripped) < 40:
        signals.append("body_too_short")

    positive = sum([
        clear_reproduction, clear_expected, code_signal, test_signal,
        code_location, actionable,
    ])
    confidence = min(0.95, 0.35 + 0.10 * positive)
    if labels_norm & GOOD_LABELS:
        confidence = min(0.95, confidence + 0.10)

    return TriageAssessment(
        actionable=actionable,
        reproducible=clear_reproduction,
        clear_expected_behavior=clear_expected,
        likely_code_change=code_signal and not docs_only,
        likely_test_change=test_signal,
        container_testable=not host_only,
        requires_maintainer_decision=maintainer,
        requires_external_service=external,
        estimated_complexity=complexity,
        confidence=round(confidence, 2),
        clear_reproduction=clear_reproduction,
        existing_relevant_tests=test_signal,
        clear_location_in_code=code_location,
        obvious_acceptance_condition=clear_expected and (clear_reproduction or test_signal),
        signals=signals,
    )
