import httpx

from flowtrack.core.credentials import load_credentials


class GitHubClient:
    BASE_URL = "https://api.github.com"

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
