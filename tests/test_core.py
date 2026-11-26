from unittest.mock import MagicMock

import pytest
import requests

import github_client as gh
from analyzer import build_prompt, normalize


@pytest.mark.parametrize("text, expected", [
    ("streamlit/streamlit", ("streamlit", "streamlit")),
    ("https://github.com/streamlit/streamlit", ("streamlit", "streamlit")),
    ("github.com/psf/requests/", ("psf", "requests")),
    ("https://www.github.com/a-b/my.repo_x.git", ("a-b", "my.repo_x")),
    ("https://github.com/owner/repo/tree/main/src", ("owner", "repo")),
    ("  owner/repo  ", ("owner", "repo")),
])
def test_parse_valid(text, expected):
    assert gh.parse_repo(text) == expected


@pytest.mark.parametrize("text", [
    "", "streamlit", "https://gitlab.com/a/b", "not a url", "a/b/c d", "https://github.com/onlyowner",
])
def test_parse_invalid(text):
    with pytest.raises(gh.InvalidRepoURL):
        gh.parse_repo(text)


def _client_returning(status, headers=None, text=""):
    client = gh.GitHubClient()
    resp = MagicMock(status_code=status, headers=headers or {}, text=text)
    client.session.get = MagicMock(return_value=resp)
    return client


def test_404_raises_not_found():
    with pytest.raises(gh.RepoNotFound):
        _client_returning(404).get_metadata("x", "y")


def test_optional_404_returns_none():
    assert _client_returning(404).get_readme("x", "y") is None


def test_rate_limit_includes_reset():
    client = _client_returning(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"})
    with pytest.raises(gh.RateLimited) as exc:
        client.get_metadata("x", "y")
    assert exc.value.reset_at.year == 2023


def test_rate_limit_hit_even_on_optional_call():
    client = _client_returning(429, {"X-RateLimit-Remaining": "0"})
    with pytest.raises(gh.RateLimited):
        client.get_readme("x", "y")


def test_timeout_becomes_network_error():
    client = gh.GitHubClient()
    client.session.get = MagicMock(side_effect=requests.Timeout())
    with pytest.raises(gh.NetworkError):
        client.get_metadata("x", "y")


def test_token_sets_bearer_header():
    assert gh.GitHubClient("abc").session.headers["Authorization"] == "Bearer abc"
    assert "Authorization" not in gh.GitHubClient().session.headers


def test_prompt_truncates_readme_and_tree():
    data = gh.RepoData(owner="o", name="r", metadata={}, readme="x" * 50_000,
                       tree=[f"f{i}.py" for i in range(1000)])
    prompt = build_prompt(data)
    assert "x" * 6001 not in prompt
    assert "f199.py" in prompt and "f200.py" not in prompt


def test_normalize_handles_loose_output():
    out = normalize({"summary": "s", "tech_stack": "Python",
                     "recommendations": ["Add tests", {"title": "CI", "priority": "high"}]})
    assert out["tech_stack"] == ["Python"]
    assert out["recommendations"][0]["title"] == "Add tests"
    assert out["recommendations"][1]["priority"] == "high"
    assert out["contribution_opportunities"] == []
