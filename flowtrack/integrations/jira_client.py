import base64

import httpx

from flowtrack.core.settings import settings


class JiraClient:
    def _headers(self) -> dict[str, str]:
        credentials = base64.b64encode(
            f"{settings.jira_email}:{settings.jira_token}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        }

    def _is_configured(self) -> bool:
        return bool(settings.jira_base_url and settings.jira_email and settings.jira_token)

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str | None = None,
        issue_type: str = "Task",
        priority: str | None = None,
    ) -> str | None:
        """Create a Jira issue. Returns the issue key (e.g. 'PROJ-123') or None on failure."""
        if not self._is_configured():
            return None

        url = f"{settings.jira_base_url}/rest/api/3/issue"

        desc_content = []
        if description:
            desc_content = [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}],
                }
                for line in description.split("\n")
                if line.strip()
            ]

        payload: dict = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "issuetype": {"name": issue_type},
            }
        }

        if desc_content:
            payload["fields"]["description"] = {
                "version": 1,
                "type": "doc",
                "content": desc_content,
            }

        if priority:
            payload["fields"]["priority"] = {"name": priority}

        try:
            response = httpx.post(url, json=payload, headers=self._headers(), timeout=10)
            if response.status_code == 201:
                return response.json().get("key")
            return None
        except httpx.HTTPError:
            return None

    def post_comment(self, ticket_id: str, body: str) -> bool:
        if not self._is_configured():
            return False

        url = f"{settings.jira_base_url}/rest/api/3/issue/{ticket_id}/comment"

        adf_body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": line}],
                    }
                    for line in body.split("\n")
                    if line.strip()
                ],
            }
        }

        try:
            response = httpx.post(url, json=adf_body, headers=self._headers(), timeout=10)
            return response.status_code in (200, 201)
        except httpx.HTTPError:
            return False
