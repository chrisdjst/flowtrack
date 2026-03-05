import httpx

from flowtrack.core.settings import settings


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def post_comment(self, pr_number: int, body: str) -> bool:
        if not settings.github_token or not settings.github_owner or not settings.github_repo:
            return False

        url = (
            f"{self.BASE_URL}/repos/{settings.github_owner}/"
            f"{settings.github_repo}/issues/{pr_number}/comments"
        )
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            response = httpx.post(url, json={"body": body}, headers=headers, timeout=10)
            return response.status_code == 201
        except httpx.HTTPError:
            return False
