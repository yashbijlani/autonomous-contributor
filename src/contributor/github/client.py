"""GitHub REST client (httpx). Abstract interface so MCP or mock can be swapped in.

Rate-limit handling: on 403 with rate-limit headers, raises RateLimitedError
with retry_after so callers can back off instead of spinning.
"""
from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from contributor.observability.logging import get_logger

log = get_logger("contributor.github.client")


class RateLimitedError(RuntimeError):
    def __init__(self, message: str, retry_after: float = 60.0):
        super().__init__(message)
        self.retry_after = retry_after


class GitHubClientProtocol(Protocol):
    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]: ...
    def get_repo(self, owner: str, repo: str) -> dict[str, Any]: ...
    def list_issues(self, owner: str, repo: str, **kwargs) -> list[dict[str, Any]]: ...
    def search_issues(self, query: str, **kwargs) -> list[dict[str, Any]]: ...
    def create_pr(self, owner: str, repo: str, *, title: str, head: str, base: str, body: str) -> dict[str, Any]: ...
    def update_pr(self, owner: str, repo: str, number: int, **kwargs) -> dict[str, Any]: ...
    def list_pr_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]: ...
    def get_ci_status(self, owner: str, repo: str, sha: str) -> dict[str, Any]: ...
    def post_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]: ...


class GitHubClient:
    def __init__(self, token: str = "", api_base: str = "https://api.github.com", timeout: float = 30.0):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = self.api_base + path
        tries = 4
        for attempt in range(tries):
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    r = c.request(method, url, headers=self._headers(), **kwargs)
            except httpx.HTTPError as e:
                if attempt == tries - 1:
                    raise RuntimeError(f"GitHub network failure {method} {path}: {e}") from e
                time.sleep(2**attempt)
                continue
            if r.status_code == 403 and "rate limit" in r.text.lower():
                retry_after = float(r.headers.get("Retry-After", "60"))
                raise RateLimitedError("GitHub rate limited", retry_after)
            if r.status_code in (502, 503, 504) and attempt < tries - 1:
                time.sleep(2**attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"GitHub API {r.status_code} {method} {path}: {r.text[:500]}")
            if not r.text:
                return {}
            return r.json()
        raise RuntimeError("unreachable")

    # ---- concrete ops ----
    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repo}")

    def get_file(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None:
        try:
            data = self._request("GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
            import base64

            if isinstance(data, dict) and data.get("encoding") == "base64":
                return base64.b64decode(data["content"]).decode(errors="replace")
            return None
        except RuntimeError:
            return None

    def get_tree(self, owner: str, repo: str, ref: str = "HEAD", recursive: bool = True) -> list[dict[str, Any]]:
        """Return the repo file tree (paths only) via the git trees API."""
        try:
            branch = self._request("GET", f"/repos/{owner}/{repo}/branches/{ref}")
            sha = ((branch or {}).get("commit") or {}).get("sha", "")
        except Exception:
            sha = ""
        if not sha:
            try:
                data = self._request("GET", f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": "1"})
                return data.get("tree", []) if isinstance(data, dict) else []
            except Exception:
                return []
        data = self._request("GET", f"/repos/{owner}/{repo}/git/trees/{sha}", params={"recursive": "1" if recursive else "0"})
        return data.get("tree", []) if isinstance(data, dict) else []

    def list_issues(self, owner: str, repo: str, **kwargs) -> list[dict[str, Any]]:
        params = {"state": "open", "per_page": 30, **kwargs}
        data = self._request("GET", f"/repos/{owner}/{repo}/issues", params=params)
        return data if isinstance(data, list) else []

    def search_issues(self, query: str, **kwargs) -> list[dict[str, Any]]:
        params = {"q": query, "per_page": kwargs.get("per_page", 30)}
        data = self._request("GET", "/search/issues", params=params)
        return data.get("items", []) if isinstance(data, dict) else []

    def create_pr(self, owner: str, repo: str, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        return self._request("POST", f"/repos/{owner}/{repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})

    def update_pr(self, owner: str, repo: str, number: int, **kwargs) -> dict[str, Any]:
        return self._request("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json=kwargs)

    def list_pr_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        try:
            data = self._request("GET", f"/repos/{owner}/{repo}/issues/{number}/comments")
            return data if isinstance(data, list) else []
        except RuntimeError:
            return []

    def list_review_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        try:
            data = self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}/comments")
            return data if isinstance(data, list) else []
        except RuntimeError:
            return []

    def get_ci_status(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        try:
            combined = self._request("GET", f"/repos/{owner}/{repo}/commits/{sha}/status")
        except RuntimeError:
            combined = {}
        try:
            runs = self._request("GET", f"/repos/{owner}/{repo}/commits/{sha}/check-runs", params={})
            check_runs = runs.get("check_runs", []) if isinstance(runs, dict) else []
        except RuntimeError:
            check_runs = []
        return {"combined": combined, "check_runs": check_runs}

    def post_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
        return self._request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})
