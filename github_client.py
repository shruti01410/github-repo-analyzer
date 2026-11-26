"""Thin wrapper around the GitHub REST API (v3).

All network access to GitHub lives here so the UI and the LLM layer never
deal with HTTP details. Errors are normalised into a small exception
hierarchy that the dashboard turns into friendly messages.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

API_ROOT = "https://api.github.com"
TIMEOUT = 15  # seconds
PACKAGE_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "setup.py",
    "Cargo.toml",
    "go.mod",
)
MAX_PACKAGE_FILE_CHARS = 3000

# owner/repo rules: owner is alnum + hyphen, repo is alnum + . _ -
_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?(?:[#?].*)?$"
)
_REPO_WITH_PATH_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?(?:/.*)?$"
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class GitHubError(Exception):
    """Base class for all GitHub client errors."""


class InvalidRepoURL(GitHubError):
    pass


class RepoNotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, reset_at: datetime | None):
        self.reset_at = reset_at
        super().__init__("GitHub rate limit reached")


class BadToken(GitHubError):
    pass


class NetworkError(GitHubError):
    pass


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_repo(text: str) -> tuple[str, str]:
    """Return (owner, repo) from 'owner/repo' or any github.com repo URL.

    Accepts URLs that point deeper into a repo (e.g. /tree/main/src).
    """
    text = (text or "").strip()
    match = _REPO_RE.match(text) or _REPO_WITH_PATH_RE.match(text)
    if not match:
        raise InvalidRepoURL(text)
    owner, repo = match.group("owner"), match.group("repo")
    if repo in {".", ".."}:
        raise InvalidRepoURL(text)
    return owner, repo


# --------------------------------------------------------------------------- #
# Data container
# --------------------------------------------------------------------------- #
@dataclass
class RepoData:
    owner: str
    name: str
    metadata: dict
    languages: dict[str, int] = field(default_factory=dict)
    readme: str | None = None
    tree: list[str] = field(default_factory=list)
    tree_truncated: bool = False
    package_files: dict[str, str] = field(default_factory=dict)
    good_first_issues: list[dict] = field(default_factory=list)
    rate_limit_remaining: int | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class GitHubClient:
    def __init__(self, token: str | None = None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "repo-ai-analyzer",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token.strip()}"
        self.rate_limit_remaining: int | None = None

    # -- low level ---------------------------------------------------------- #
    def _get(self, path: str, params: dict | None = None, optional: bool = False):
        """GET a GitHub API path. Returns parsed JSON, or None for optional 404s."""
        try:
            resp = self.session.get(f"{API_ROOT}{path}", params=params, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise NetworkError(str(exc)) from exc

        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code in (403, 429) and (
            remaining == "0" or "rate limit" in resp.text.lower()
        ):
            reset = resp.headers.get("X-RateLimit-Reset")
            reset_at = (
                datetime.fromtimestamp(int(reset), tz=timezone.utc) if reset else None
            )
            raise RateLimited(reset_at)

        if resp.status_code == 401:
            raise BadToken("The GitHub token was rejected")

        if resp.status_code in (404, 409, 451) and optional:
            # 409 = empty repository (no commits), 451 = legal block on a file
            return None

        if resp.status_code == 404:
            raise RepoNotFound(path)

        if optional:
            return None
        raise GitHubError(f"GitHub returned HTTP {resp.status_code} for {path}")

    # -- endpoints ---------------------------------------------------------- #
    def get_metadata(self, owner: str, repo: str) -> dict:
        return self._get(f"/repos/{owner}/{repo}")

    def get_languages(self, owner: str, repo: str) -> dict[str, int]:
        return self._get(f"/repos/{owner}/{repo}/languages", optional=True) or {}

    def get_readme(self, owner: str, repo: str) -> str | None:
        data = self._get(f"/repos/{owner}/{repo}/readme", optional=True)
        return _decode_content(data)

    def get_tree(self, owner: str, repo: str, branch: str) -> tuple[list[str], bool]:
        data = self._get(
            f"/repos/{owner}/{repo}/git/trees/{branch}",
            params={"recursive": "1"},
            optional=True,
        )
        if not data:
            return [], False
        paths = [
            item["path"] + ("/" if item["type"] == "tree" else "")
            for item in data.get("tree", [])
        ]
        return paths, bool(data.get("truncated"))

    def get_file(self, owner: str, repo: str, path: str) -> str | None:
        data = self._get(f"/repos/{owner}/{repo}/contents/{path}", optional=True)
        if isinstance(data, list):  # path is a directory
            return None
        return _decode_content(data)

    def get_good_first_issues(self, owner: str, repo: str, limit: int = 10) -> list[dict]:
        data = self._get(
            f"/repos/{owner}/{repo}/issues",
            params={"labels": "good first issue", "state": "open", "per_page": limit},
            optional=True,
        ) or []
        # The issues endpoint also returns PRs; keep real issues only.
        return [
            {"title": i["title"], "url": i["html_url"], "number": i["number"]}
            for i in data
            if "pull_request" not in i
        ]

    # -- orchestration ------------------------------------------------------ #
    def fetch_repo(self, owner: str, repo: str) -> RepoData:
        """Fetch everything the analyzer needs. Only metadata is mandatory."""
        meta = self.get_metadata(owner, repo)
        owner, repo = meta["owner"]["login"], meta["name"]  # canonical casing
        branch = meta.get("default_branch") or "main"

        tree, truncated = self.get_tree(owner, repo, branch)
        tree_set = set(tree)

        packages: dict[str, str] = {}
        for name in PACKAGE_FILES:
            # Skip a request when the tree proves the file is absent.
            if tree and not truncated and name not in tree_set:
                continue
            content = self.get_file(owner, repo, name)
            if content:
                packages[name] = content[:MAX_PACKAGE_FILE_CHARS]

        return RepoData(
            owner=owner,
            name=repo,
            metadata=meta,
            languages=self.get_languages(owner, repo),
            readme=self.get_readme(owner, repo),
            tree=tree,
            tree_truncated=truncated,
            package_files=packages,
            good_first_issues=self.get_good_first_issues(owner, repo),
            rate_limit_remaining=self.rate_limit_remaining,
        )


def _decode_content(data: dict | None) -> str | None:
    if not data or data.get("encoding") != "base64" or not data.get("content"):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return None
