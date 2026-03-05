import base64

import httpx

from flowtrack.core.settings import settings


class JiraClient:
    def post_comment(self, ticket_id: str, body: str) -> bool:
        if not settings.jira_base_url or not settings.jira_email or not settings.jira_token:
            return False

        url = f"{settings.jira_base_url}/rest/api/3/issue/{ticket_id}/comment"
        credentials = base64.b64encode(
            f"{settings.jira_email}:{settings.jira_token}".encode()
        ).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        }

        # Atlassian Document Format (ADF)
        adf_body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": line}
                        ],
                    }
                    for line in body.split("\n")
                    if line.strip()
                ],
            }
        }

        try:
            response = httpx.post(url, json=adf_body, headers=headers, timeout=10)
            return response.status_code in (200, 201)
        except httpx.HTTPError:
            return False
