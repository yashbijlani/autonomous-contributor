"""Unit: deterministic PR target resolution, reuse and truthful PR bodies."""
from contributor.config import Settings
from contributor.github.pull_requests import (
    find_existing_pr,
    publish_pr,
    render_pr_body,
    render_pr_title,
    resolve_pr_target,
)
from contributor.models.state import (
    JobState,
    ReviewResult,
    ReviewVerdict,
    TestResult,
    TriageDecision,
    TriageResult,
)


class FakeClient:
    def __init__(self, *, login="alice", existing=None, template=""):
        self.login = login
        self.existing = existing
        self.template = template
        self.created = []
        self.updated = []
        self.prs = []

    def get_authenticated_user(self):
        return {"login": self.login}

    def get_file(self, owner, repo, path, ref="HEAD"):
        if self.template and "pull_request_template" in path.lower():
            return self.template
        return None

    def list_pull_requests(self, owner, repo, *, head="", state="all"):
        if self.existing:
            return [self.existing]
        return list(self.prs)

    def create_pr(self, owner, repo, *, title, head, base, body):
        pr = {"number": len(self.created) + 1, "html_url": f"https://github.com/{owner}/{repo}/pull/{len(self.created)+1}",
              "title": title, "head": head, "base": base, "body": body, "state": "open"}
        self.created.append(pr)
        self.prs.append(pr)
        return pr

    def update_pr(self, owner, repo, number, **kwargs):
        self.updated.append({"number": number, **kwargs})
        return {"number": number, "html_url": f"https://github.com/{owner}/{repo}/pull/{number}"}


def _job(repo="omacom/omarchy", branch="contrib/issue-1-abcd", **kw):
    return JobState(
        job_id="j1", repository=repo, issue_number=12665,
        issue_title="Fix symlink search", branch_name=branch,
        triage_result=TriageResult(decision=TriageDecision.ACCEPT, reason="ok"),
        **kw,
    )


def test_target_same_repo_when_push_target_matches():
    t = resolve_pr_target(_job(), push_target="omacom/omarchy", base_branch="quattro")
    assert t.head_owner == ""
    assert t.head_ref == "contrib/issue-1-abcd"
    assert t.base_branch == "quattro"
    assert t.base_repo == "omacom/omarchy"


def test_target_uses_fork_owner_dynamically():
    t = resolve_pr_target(_job(), push_target="yashbijlani/omarchy", base_branch="quattro")
    assert t.head_owner == "yashbijlani"
    assert t.head_ref == "yashbijlani:contrib/issue-1-abcd"
    assert t.base_repo == "omacom/omarchy"


def test_target_same_owner_fork_is_same_repo_head():
    t = resolve_pr_target(_job(), push_target="omacom/omarchy", base_branch="main")
    assert t.head_ref == "contrib/issue-1-abcd"


def test_pr_title_mentions_issue():
    assert render_pr_title(_job()) == "Fix #12665: Fix symlink search"


def test_render_body_is_truthful_about_targeted_tests():
    st = _job()
    st.test_results = [TestResult(command="bash test/a.sh", exit_code=0, passed=True, duration_s=1.0)]
    st.issue_metadata["targeted_test_command"] = "bash test/a.sh"
    body = render_pr_body(st, Settings(pr_ai_disclosure=True))
    assert "bash test/a.sh" in body
    assert "full repository suite was not run" in body
    assert "AI assistance" in body


def test_render_body_has_no_local_paths_or_secrets():
    st = _job()
    st.workspace_path = "/home/someone/.workspaces/job-1"
    body = render_pr_body(st, Settings())
    assert "/home/someone" not in body
    assert "GITHUB_TOKEN" not in body


def test_render_body_appends_to_repo_template():
    st = _job()
    client = FakeClient(template="## Description\n\n<!-- required -->\n")
    body = render_pr_body(st, Settings(pr_use_template=True), client=client, base_repo=st.repository)
    assert "## Description" in body and "<!-- required -->" in body
    assert "## Summary" in body
    assert st.pr_template_used


def test_publish_pr_creates_with_fork_head():
    st = _job()
    client = FakeClient(login="yashbijlani")
    data, action, target = publish_pr(client, st, Settings(ci_verify_remote=False),
                                      push_target="yashbijlani/omarchy", base_branch="quattro")
    assert action == "created"
    assert client.created[0]["head"] == "yashbijlani:contrib/issue-1-abcd"
    assert client.created[0]["base"] == "quattro"
    assert st.pull_request_number == 1
    assert st.pull_request_url


def test_publish_pr_same_repo_direct_push_head():
    st = _job()
    client = FakeClient()
    _, action, target = publish_pr(client, st, Settings(), push_target="omacom/omarchy", base_branch="quattro")
    assert action == "created"
    assert ":" not in client.created[0]["head"]


def test_publish_pr_reuses_existing_pr():
    existing = {"number": 7, "html_url": "https://github.com/omacom/omarchy/pull/7", "state": "open",
                "head": "contrib/issue-1-abcd"}
    st = _job()
    client = FakeClient(existing=existing)
    data, action, _ = publish_pr(client, st, Settings(), push_target="omacom/omarchy")
    assert action == "reused"
    assert st.pr_reused
    assert st.pull_request_number == 7
    assert client.created == []
    assert client.updated and client.updated[0]["number"] == 7


def test_publish_pr_updates_when_number_already_known():
    st = _job()
    st.pull_request_number = 3
    st.pull_request_url = "https://github.com/omacom/omarchy/pull/3"
    client = FakeClient()
    data, action, _ = publish_pr(client, st, Settings(), push_target="omacom/omarchy")
    assert action == "updated"
    assert client.created == []
    assert client.updated[0]["number"] == 3


def test_find_existing_pr_prefers_open():
    existing = {"number": 2, "state": "open", "head": "b"}
    client = FakeClient(existing=existing)
    assert find_existing_pr(client, "o/r", "b")["number"] == 2
    assert find_existing_pr(client, "o/r", "") is None
