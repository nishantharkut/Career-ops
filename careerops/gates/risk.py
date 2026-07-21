from dataclasses import asdict, dataclass
import re
from typing import Any


@dataclass(frozen=True)
class RiskIssue:
    severity: str
    label: str
    source: str
    why: str
    next_action: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RiskReport:
    blockers: list[RiskIssue]
    warnings: list[RiskIssue]

    def to_dict(self) -> dict[str, object]:
        return {
            "blockers": [issue.to_dict() for issue in self.blockers],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


METRIC_PATTERN = re.compile(r"(\d+%|\d+\+|\b\d+\s+regression\b)", re.IGNORECASE)
SECRET_PATTERN = re.compile(
    r"(?i)(password|secret|token|api(?:[\s_-]*key))\s*[:=]\s*\S+"
)
NDA_TERMS = (
    "cdi testing",
    "oig exclusion",
    "eob_era_new",
    "eob era",
    "rcm_v2",
    "accurio",
)


def evaluate_risks(
    *,
    resume_text: str,
    claims: list[dict[str, Any]],
    pdf_is_stale: bool = False,
    pdf_parse_ok: bool = True,
    page_count: int | None = 1,
) -> RiskReport:
    blockers: list[RiskIssue] = []
    warnings: list[RiskIssue] = []
    lowered = resume_text.lower()

    if pdf_is_stale:
        blockers.append(
            _issue(
                "PDF is stale",
                "resume",
                "The LaTeX source changed after the selected PDF was rendered.",
                "Render PDF again before approval.",
            )
        )
    if not pdf_parse_ok:
        blockers.append(
            _issue(
                "PDF text not parseable",
                "resume",
                "The rendered PDF text layer could not be extracted.",
                "Fix the PDF render before approval.",
            )
        )
    if page_count is not None and page_count > 1:
        blockers.append(
            _issue(
                "Resume exceeds one page",
                "resume",
                "The v0 resume gate requires one page unless explicitly allowed.",
                "Reduce the resume or record an application-specific exception.",
            )
        )

    if any(term in lowered for term in NDA_TERMS):
        blockers.append(
            _issue(
                "NDA-sensitive wording",
                "resume",
                "The resume names internal or sensitive work areas.",
                "Rewrite the claim using public-safe wording before approval.",
            )
        )

    if "quickintell" in lowered and "ai engineering intern" in lowered and "software engineering intern, ai systems" not in lowered:
        blockers.append(
            _issue(
                "Unsupported title",
                "resume",
                "QuickIntell title is unresolved and must not be overclaimed.",
                "Use the verified title or a safer combined title.",
            )
        )

    if SECRET_PATTERN.search(resume_text):
        blockers.append(
            _issue(
                "Secret-like text",
                "resume",
                "The artifact contains text shaped like a credential.",
                "Remove the secret-like value and rotate it if real.",
            )
        )

    for claim in claims:
        text = str(claim.get("text", ""))
        verification_state = str(claim.get("verification_state", "needs_verification"))
        public_safe = bool(claim.get("public_safe", True))
        if METRIC_PATTERN.search(text) and verification_state != "verified":
            blockers.append(
                _issue(
                    "Missing evidence",
                    "claim",
                    "Metric claims need evidence before they can be used.",
                    "Link repository, log, artifact, or user-approved evidence.",
                )
            )
        if verification_state in {"needs_verification", "rejected", "private_do_not_publish"}:
            blockers.append(
                _issue(
                    "Claim not verified",
                    "claim",
                    "A resume claim is not verified for public use.",
                    "Verify, rewrite, or remove the claim before approval.",
                )
            )
        elif verification_state == "user_stated":
            warnings.append(
                _issue(
                    "User-stated claim",
                    "claim",
                    "The claim is user-stated but not externally verified.",
                    "Keep only if the user accepts this warning during approval.",
                    severity="warning",
                )
            )
        if not public_safe:
            blockers.append(
                _issue(
                    "Private claim",
                    "claim",
                    "A private-only claim appears in public resume text.",
                    "Remove or rewrite the claim.",
                )
            )

    return RiskReport(blockers=_dedupe(blockers), warnings=_dedupe(warnings))


def _issue(label: str, source: str, why: str, next_action: str, severity: str = "blocker") -> RiskIssue:
    return RiskIssue(
        severity=severity,
        label=label,
        source=source,
        why=why,
        next_action=next_action,
    )


def _dedupe(issues: list[RiskIssue]) -> list[RiskIssue]:
    seen: set[tuple[str, str]] = set()
    result: list[RiskIssue] = []
    for issue in issues:
        key = (issue.label, issue.source)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
