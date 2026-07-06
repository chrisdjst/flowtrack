import base64

import httpx

from flowtrack.core.credentials import load_credentials


class JiraClient:
    def _get_creds(self) -> dict[str, str]:
        return load_credentials("jira_base_url", "jira_email", "jira_token")

    def _headers(self, creds: dict[str, str]) -> dict[str, str]:
        credentials = base64.b64encode(
            f"{creds['jira_email']}:{creds['jira_token']}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        }

    def _is_configured(self, creds: dict[str, str]) -> bool:
        return bool(creds.get("jira_base_url") and creds.get("jira_email") and creds.get("jira_token"))

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str | None = None,
        issue_type: str = "Task",
        priority: str | None = None,
    ) -> str | None:
        """Create a Jira issue. Returns the issue key (e.g. 'PROJ-123') or None on failure."""
        creds = self._get_creds()
        if not self._is_configured(creds):
            return None

        url = f"{creds['jira_base_url']}/rest/api/3/issue"

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
            response = httpx.post(url, json=payload, headers=self._headers(creds), timeout=10)
            if response.status_code == 201:
                return response.json().get("key")
            return None
        except httpx.HTTPError:
            return None

    def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        """Run a JQL query and return the raw issue payloads. Empty on error.

        Used by the discovery scheduler to pull labeled backlog items. Caller
        decides what to do with the payload — this method stays Jira-shaped.
        """
        creds = self._get_creds()
        if not self._is_configured(creds):
            return []
        url = f"{creds['jira_base_url']}/rest/api/3/search/jql"
        payload = {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "description", "priority", "status", "labels", "issuetype"],
        }
        try:
            response = httpx.post(url, json=payload, headers=self._headers(creds), timeout=15)
            if response.status_code != 200:
                return []
            return response.json().get("issues", [])
        except httpx.HTTPError:
            return []

    def get_transitions(self, ticket_id: str) -> list[dict]:
        """Return available transitions for an issue."""
        creds = self._get_creds()
        if not self._is_configured(creds):
            return []
        url = f"{creds['jira_base_url']}/rest/api/3/issue/{ticket_id}/transitions"
        try:
            resp = httpx.get(url, headers=self._headers(creds), timeout=10)
            return resp.json().get("transitions", []) if resp.status_code == 200 else []
        except httpx.HTTPError:
            return []

    def transition_issue(self, ticket_id: str, *candidate_names: str) -> bool:
        """Move an issue to the first matching transition name (case-insensitive).

        Accepts multiple candidate names so callers can provide fallbacks for
        different Jira workflow configurations (e.g. "In Review" / "Code Review").
        Returns True on first successful transition.
        """
        transitions = self.get_transitions(ticket_id)
        if not transitions:
            return False
        by_name = {t["name"].lower(): t["id"] for t in transitions}
        creds = self._get_creds()
        url = f"{creds['jira_base_url']}/rest/api/3/issue/{ticket_id}/transitions"
        for name in candidate_names:
            tid = by_name.get(name.lower())
            if tid is None:
                continue
            try:
                resp = httpx.post(
                    url,
                    json={"transition": {"id": tid}},
                    headers=self._headers(creds),
                    timeout=10,
                )
                if resp.status_code == 204:
                    return True
            except httpx.HTTPError:
                pass
        return False

    def post_comment(self, ticket_id: str, body: str) -> bool:
        creds = self._get_creds()
        if not self._is_configured(creds):
            return False

        url = f"{creds['jira_base_url']}/rest/api/3/issue/{ticket_id}/comment"

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
            response = httpx.post(url, json=adf_body, headers=self._headers(creds), timeout=10)
            return response.status_code in (200, 201)
        except httpx.HTTPError:
            return False
