"""SecOps scan: dependency vulnerabilities + exposed secrets as discovery items.

Two scanners, both pure functions over a repo path so they're testable
without the source wrapper:

- ``scan_secrets``: regex sweep for well-known credential shapes over
  git-tracked text files. Every hit is CRITICAL — a committed credential is
  live until rotated. Matched values are never stored; refs use a hash.
- ``scan_dependencies``: exact version pins (uv.lock, requirements*.txt)
  queried against the OSV.dev API. Severity comes from the advisory
  (GHSA-style CRITICAL/HIGH/MODERATE/LOW). Range specifiers (>=) are
  skipped — without a resolved version any answer would be a guess.

Findings become DiscoveryCandidates (kind=security). CRITICAL ones carry
``critical=True`` so the manager escalates them straight to a blocked task
(see promote.escalate_critical_item); the rest wait in the TPM funnel.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx

from flowtrack.discovery.base import DiscoveryCandidate
from flowtrack.models.discovered_item import DiscoveryKind, DiscoverySource

log = logging.getLogger(__name__)

OSV_API = "https://api.osv.dev/v1"
_HTTP_TIMEOUT = 10.0
_MAX_FILE_BYTES = 256 * 1024
_MAX_VULN_DETAILS = 20  # per scan, keeps the OSV detail fetches bounded

_SEVERITY_SCORE: dict[str, int] = {
    "CRITICAL": 999,
    "HIGH": 700,
    "MODERATE": 400,
    "LOW": 100,
    "UNKNOWN": 250,
}

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY( BLOCK)?-----")),
)


@dataclass(slots=True, frozen=True)
class SecOpsFinding:
    scanner: str          # "secrets" | "dependencies"
    ref: str              # stable id -> discovered_items idempotency
    title: str
    summary: str | None
    severity: str         # CRITICAL | HIGH | MODERATE | LOW | UNKNOWN
    raw: dict


# --------------------------------------------------------------------------
# secret scanner
# --------------------------------------------------------------------------

def _tracked_files(repo_path: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=repo_path,
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        log.warning("secops secrets: git ls-files failed in %s: %s", repo_path, out.stderr.strip())
        return []
    return [repo_path / line for line in out.stdout.splitlines() if line]


def scan_secrets(repo_path: Path) -> list[SecOpsFinding]:
    findings: list[SecOpsFinding] = []
    for path in _tracked_files(repo_path):
        try:
            if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
                continue
            head = path.read_bytes()
        except OSError:
            continue
        if b"\0" in head[:1024]:  # binary
            continue
        text = head.decode("utf-8", errors="ignore")
        rel = path.relative_to(repo_path).as_posix()
        for name, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(text):
                secret = match.group(0)
                # Never persist the credential itself — hash for identity,
                # short prefix for the human to locate it.
                digest = hashlib.sha256(secret.encode()).hexdigest()[:12]
                line_no = text.count("\n", 0, match.start()) + 1
                findings.append(SecOpsFinding(
                    scanner="secrets",
                    ref=f"secret:{rel}:{name}:{digest}",
                    title=f"Exposed {name} in {rel}",
                    summary=(
                        f"{rel}:{line_no} matches the {name} pattern "
                        f"(value starts with '{secret[:4]}…', sha256 {digest}). "
                        "Rotate the credential and purge it from history."
                    ),
                    severity="CRITICAL",
                    raw={"file": rel, "line": line_no, "pattern": name, "sha256_12": digest},
                ))
    return findings


# --------------------------------------------------------------------------
# dependency scanner
# --------------------------------------------------------------------------

def _collect_pins(repo_path: Path) -> dict[str, str]:
    """Exact PyPI pins from uv.lock and requirements*.txt. Name -> version."""
    pins: dict[str, str] = {}
    lock = repo_path / "uv.lock"
    if lock.is_file():
        try:
            data = tomllib.loads(lock.read_text(encoding="utf-8"))
            for pkg in data.get("package", []):
                name, version = pkg.get("name"), pkg.get("version")
                if name and version:
                    pins[name] = version
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("secops deps: could not parse uv.lock: %s", exc)
    pin_re = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*==\s*([A-Za-z0-9.!+_-]+)")
    for req in sorted(repo_path.glob("requirements*.txt")):
        try:
            lines = req.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            m = pin_re.match(line.split("#", 1)[0])
            if m:
                pins.setdefault(m.group(1).lower(), m.group(2))
    return pins


def _advisory_severity(vuln: dict) -> str:
    sev = ((vuln.get("database_specific") or {}).get("severity") or "").upper()
    return sev if sev in _SEVERITY_SCORE else "UNKNOWN"


def scan_dependencies(repo_path: Path) -> list[SecOpsFinding]:
    pins = _collect_pins(repo_path)
    if not pins:
        log.info("secops deps: no exact pins found in %s, skipping", repo_path)
        return []

    items = sorted(pins.items())
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(f"{OSV_API}/querybatch", json={"queries": [
            {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
            for name, version in items
        ]})
        resp.raise_for_status()
        results = resp.json().get("results", [])

        findings: list[SecOpsFinding] = []
        detail_budget = _MAX_VULN_DETAILS
        for (name, version), result in zip(items, results):
            for hit in (result or {}).get("vulns", []):
                vuln_id = hit.get("id")
                if not vuln_id:
                    continue
                vuln: dict = hit
                if detail_budget > 0:
                    try:
                        vuln = client.get(f"{OSV_API}/vulns/{vuln_id}").json()
                    except httpx.HTTPError:
                        pass
                    detail_budget -= 1
                severity = _advisory_severity(vuln)
                findings.append(SecOpsFinding(
                    scanner="dependencies",
                    ref=f"dep:{name}:{version}:{vuln_id}",
                    title=f"{vuln_id}: {name} {version} is vulnerable",
                    summary=(vuln.get("summary") or vuln.get("details") or "")[:1000] or None,
                    severity=severity,
                    raw={"package": name, "version": version, "vuln_id": vuln_id,
                         "severity": severity, "aliases": vuln.get("aliases", [])},
                ))
    return findings


# --------------------------------------------------------------------------
# source
# --------------------------------------------------------------------------

class SecOpsScanSource:
    name = "secops"

    def __init__(
        self,
        *,
        repo_path: Path | str | None = None,
        scanners: list | None = None,
        max_findings: int | None = None,
        interval_seconds: int | None = None,
    ) -> None:
        from flowtrack.core.runtime_config import RuntimeConfig

        target = repo_path or RuntimeConfig.get("target_repo_path") or "."
        self.repo_path = Path(target).resolve()
        self.scanners = scanners if scanners is not None else [scan_secrets, scan_dependencies]
        self.max_findings = (
            max_findings if max_findings is not None
            else RuntimeConfig.get("secops_max_findings")
        )
        self.interval_seconds = (
            interval_seconds if interval_seconds is not None
            else RuntimeConfig.get("secops_scan_interval_seconds")
        )

    def fetch(self) -> list[DiscoveryCandidate]:
        findings: list[SecOpsFinding] = []
        for scanner in self.scanners:
            try:
                findings.extend(scanner(self.repo_path))
            except Exception:
                log.exception("secops scanner %s crashed; continuing",
                              getattr(scanner, "__name__", scanner))
        # Worst first, so a low cap never drops a CRITICAL behind noise.
        findings.sort(key=lambda f: _SEVERITY_SCORE.get(f.severity, 0), reverse=True)
        findings = findings[: self.max_findings]

        candidates = [
            DiscoveryCandidate(
                source=DiscoverySource.SECOPS,
                source_ref=f.ref,
                kind=DiscoveryKind.SECURITY,
                title=f.title,
                summary=f.summary,
                raw_payload={**f.raw, "scanner": f.scanner, "secops_severity": f.severity},
                signal_score=Decimal(_SEVERITY_SCORE.get(f.severity, 0)),
                critical=f.severity == "CRITICAL",
            )
            for f in findings
        ]
        log.info("secops: %d findings (%d critical) in %s",
                 len(candidates), sum(c.critical for c in candidates), self.repo_path)
        return candidates
