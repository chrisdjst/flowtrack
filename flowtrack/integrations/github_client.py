import httpx

from flowtrack.core.credentials import load_credentials


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def _creds(self) -> tuple[str, str, str]:
        c = load_credentials("github_token", "github_owner", "github_repo")
        return (
            c.get("github_token", ""),
            c.get("github_owner", ""),
            c.get("github_repo", ""),
        )

    def list_issues(
        self,
        *,
        label: str | None = None,
        state: str = "open",
        per_page: int = 50,
    ) -> list[dict]:
        """Return issues for the configured repo. Empty list if not configured.

        Used by the GitHub discovery source. Note: GitHub's /issues endpoint
        also returns pull requests — caller can filter via ``issue["pull_request"]``
        being absent if PRs should be excluded.
        """
        token, owner, repo = self._creds()
        if not token or not owner or not repo:
            return []
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues"
        params: dict[str, str | int] = {"state": state, "per_page": per_page}
        if label:
            params["labels"] = label
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=15)
            if response.status_code != 200:
                return []
            return response.json()
        except httpx.HTTPError:
            return []

    def post_comment(self, pr_number: int, body: str) -> bool:
        creds = load_credentials("github_token", "github_owner", "github_repo")
        token = creds.get("github_token", "")
        owner = creds.get("github_owner", "")
        repo = creds.get("github_repo", "")

        if not token or not owner or not repo:
            return False

        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            response = httpx.post(url, json={"body": body}, headers=headers, timeout=10)
            return response.status_code == 201
        except httpx.HTTPError:
            return False
