"""Triage agent: deterministic safety + completeness gates, heuristic scoring.

Never auto-works on:
- credential/secret exposure, destructive infra, unclear product decisions,
  breaking API changes without maintainer direction, explicit human-auth requests.

The LLM may refine scoring, but the blocklist below is programmatic and cannot
be overridden by model output.
"""
from __future__ import annotations

import re

from contributor.models.state import TriageDecision, TriageResult

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
    # 6. likely files: extract paths mentioned
    likely = sorted(set(re.findall(r"[A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|rb|php)", text)))[:10]
    confidence = 0.7 if len((body or "")) > 100 else 0.55
    tier = "xhigh" if difficulty >= 4 else "high"
    return TriageResult(
        decision=TriageDecision.ACCEPT, issue_type=issue_type,  # type: ignore[arg-type]
        difficulty=difficulty, confidence=confidence, likely_files=likely,
        requires_human=False,
        reason=f"Accepted: {issue_type} (difficulty {difficulty}). Sufficient detail to attempt.",
        recommended_model=tier,  # type: ignore[arg-type]
    )
