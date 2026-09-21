"""Unit: issue parsing + normalization."""
import pytest

from contributor.github.issues import normalize_issue
from contributor.models.state import IssueRef


def test_parse_valid():
    r = IssueRef.parse("octo/repo#123")
    assert (r.owner, r.repo, r.number) == ("octo", "repo", 123)


def test_parse_invalid():
    with pytest.raises(ValueError):
        IssueRef.parse("not-a-ref")


def test_normalize():
    title, body, meta = normalize_issue({
        "title": "Bug!", "body": "details",
        "labels": [{"name": "bug"}], "state": "open",
        "user": {"login": "alice"}, "created_at": "t", "updated_at": "t",
        "comments": 2, "assignees": [],
    })
    assert title == "Bug!"
    assert meta["labels"] == ["bug"]
    assert meta["user"] == "alice"
