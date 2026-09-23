"""Unit: bounded CI polling (watch_ci) — no real sleeps."""
from contributor.github.ci import watch_ci


class SeqClient:
    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def get_ci_status(self, owner, repo, sha):
        payload = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return payload


class Clock:
    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


PASS = {"combined": {"state": "success", "statuses": []},
        "check_runs": [{"name": "t", "status": "completed", "conclusion": "success"}]}
FAIL = {"combined": {"state": "failure", "statuses": [{"context": "t", "state": "failure"}]}, "check_runs": []}
PENDING = {"combined": {"state": "pending", "statuses": []},
           "check_runs": [{"name": "t", "status": "queued", "conclusion": None}]}


def _watch(client, **kw):
    kw.setdefault("interval_s", 0)
    kw.setdefault("max_wait_s", 100)
    kw.setdefault("settle_s", 0)
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("now", Clock())
    return watch_ci(client, "o", "r", "sha", **kw)


def test_watch_pass():
    assert _watch(SeqClient([PASS])).state == "pass"


def test_watch_fail():
    assert _watch(SeqClient([FAIL])).state == "fail"


def test_watch_pending_then_pass():
    sleeps: list[float] = []
    res = _watch(SeqClient([PENDING, PENDING, PASS]), interval_s=7, sleep=lambda s: sleeps.append(s))
    assert res.state == "pass"
    assert res.polls == 3
    assert sleeps == [7, 7]


def test_watch_timeout_is_bounded():
    res = _watch(SeqClient([PENDING]), max_wait_s=2, interval_s=1)
    assert res.timed_out
    assert res.state == "pending"


def test_watch_unknown_no_checks():
    res = _watch(SeqClient([{"combined": {"state": "unknown", "statuses": []}, "check_runs": []}]), settle_s=0)
    assert res.state == "unknown"


def test_watch_pending_with_zero_checks_becomes_unknown_not_timeout():
    """GitHub reports 'pending' for a commit with no checks: settle, don't hang."""
    payload = {"combined": {"state": "pending", "statuses": [], "total_count": 0}, "check_runs": []}
    res = _watch(SeqClient([payload]), settle_s=2, max_wait_s=100, interval_s=1)
    assert res.state == "unknown"
    assert not res.timed_out


def test_watch_on_update_called():
    seen = []
    _watch(SeqClient([PENDING, PASS]), on_update=lambda ci: seen.append(ci.state))
    assert seen[0] == "pending"
    assert seen[-1] == "pass"
