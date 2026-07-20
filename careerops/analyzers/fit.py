from dataclasses import asdict, dataclass

from careerops.parsers.jd_parser import ParsedJD


@dataclass(frozen=True)
class FitReport:
    matched_requirements: list[str]
    missing_requirements: list[str]
    weak_matches: list[str]
    exact_keyword_gaps: list[str]
    score_parts: dict[str, int]
    summary_score: int
    explanations: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def analyze_fit(jd: ParsedJD, resume_text: str) -> FitReport:
    resume_lower = resume_text.lower()
    requirements = _unique(jd.required_skills)
    matched = [item for item in requirements if item.lower() in resume_lower]
    missing = [item for item in requirements if item not in matched]
    keyword_gaps = [keyword for keyword in jd.exact_keywords if keyword not in resume_lower]
    coverage = round((len(matched) / len(requirements)) * 100) if requirements else 100
    keyword_coverage = round(
        ((len(jd.exact_keywords) - len(keyword_gaps)) / len(jd.exact_keywords)) * 100
    ) if jd.exact_keywords else 100
    summary = round((coverage * 0.7) + (keyword_coverage * 0.3))

    return FitReport(
        matched_requirements=matched,
        missing_requirements=missing,
        weak_matches=[],
        exact_keyword_gaps=keyword_gaps,
        score_parts={
            "requirement_coverage": coverage,
            "exact_keyword_coverage": keyword_coverage,
        },
        summary_score=summary,
        explanations=[
            "This is a local evaluator, not a Resume Worded or employer ATS score.",
            "Requirement coverage is based on deterministic text matching.",
        ],
    )


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
