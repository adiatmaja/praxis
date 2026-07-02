import pytest

from orchestrator.core.github_credentials import (
    CredentialError,
    PatCredentialProvider,
    repo_slug_from_url,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("owner/repo", "owner/repo"),
    ],
)
def test_repo_slug_from_url(url, expected):
    assert repo_slug_from_url(url) == expected


def test_repo_slug_from_url_rejects_garbage():
    with pytest.raises(CredentialError):
        repo_slug_from_url("not-a-repo")


async def test_pat_provider_returns_static_token_for_any_repo():
    provider = PatCredentialProvider("ghp_static")
    assert await provider.token_for_repo("https://github.com/a/b") == "ghp_static"
    assert await provider.token_for_repo("c/d") == "ghp_static"
