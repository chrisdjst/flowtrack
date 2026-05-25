"""Real-Claude smoke for the PM refinement agent.

Spends ~$0.03-0.10 per run (Sonnet, structured output, one shot). Uses Claude
Code OAuth login — no API key needed.

Verifies refine_async() against two synthetic discovered items:
  - one that's clearly actionable (should recommend 'promote')
  - one that's deliberately vague/noise (should recommend 'reject')

NOT a hard pass criterion on the recommendation values — model can disagree.
Only fails if the call errors or the structured output is malformed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid as _uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)

from flowtrack.agents.pm import refine_async  # noqa: E402


async def main() -> int:
    run_tag = _uuid.uuid4().hex[:6]

    cases = [
        {
            "label": "actionable",
            "title": f"Login fails with empty password (run {run_tag})",
            "summary": (
                "Reproducible: POST /api/auth/login with username only causes "
                "an unhandled NullPointerException in AuthController:42. "
                "Expected behaviour: return 400 with error 'password required'."
            ),
            "kind": "bug",
            "source": "github_issue",
            "source_ref": f"#9999-{run_tag}",
        },
        {
            "label": "vague",
            "title": f"Make it better (run {run_tag})",
            "summary": "I think the app could be improved somehow.",
            "kind": "improvement",
            "source": "jira",
            "source_ref": f"PROJ-VAGUE-{run_tag}",
        },
    ]

    total_cost = 0.0
    failures = 0
    for case in cases:
        try:
            result = await refine_async(
                title=case["title"],
                summary=case["summary"],
                kind=case["kind"],
                source=case["source"],
                source_ref=case["source_ref"],
                timeout_seconds=60,
            )
        except Exception as e:
            print(f"[{case['label']}] ERROR: {e}")
            failures += 1
            continue
        print()
        print(f"=== {case['label']} ({case['source_ref']}) ===")
        print(f"  recommendation     = {result.recommendation}")
        print(f"  module_hint        = {result.module_hint}")
        print(f"  cost_usd           = ${result.cost_usd}")
        crit = result.acceptance_criteria
        print(f"  acceptance_criteria= {crit[:200]}{'...' if len(crit) > 200 else ''}")
        total_cost += float(result.cost_usd)

    print()
    print(f"total cost across {len(cases)} cases: ${total_cost:.4f}")
    print("RESULT:", "PASS" if failures == 0 else "FAIL")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
