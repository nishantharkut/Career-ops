from dataclasses import asdict, dataclass
import re


UNKNOWN = "Unknown"
JD_PARSER_VERSION = "careerops-jd-parser-v1"


@dataclass(frozen=True)
class ParsedJD:
    company: str
    role_title: str
    location: str
    work_mode: str
    duration: str
    required_skills: list[str]
    preferred_skills: list[str]
    responsibilities: list[str]
    eligibility: list[str]
    knockout_conditions: list[str]
    exact_keywords: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


SECTION_ALIASES = {
    "responsibilities": "responsibilities",
    "responsibility": "responsibilities",
    "required skills": "required_skills",
    "requirements": "required_skills",
    "required": "required_skills",
    "preferred skills": "preferred_skills",
    "preferred": "preferred_skills",
    "eligibility": "eligibility",
    "eligible": "eligibility",
}

FIELD_PATTERNS = {
    "company": re.compile(r"^\s*Company\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE),
    "role_title": re.compile(r"^\s*(Role|Title)\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE),
    "location": re.compile(r"^\s*Location\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE),
    "work_mode": re.compile(r"^\s*Work mode\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE),
    "duration": re.compile(r"^\s*Duration\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE),
}

KEYWORD_PHRASES = (
    "python",
    "sql",
    "machine learning",
    "data analysis",
    "statistics",
    "a/b testing",
    "c++",
    "operating systems",
    "dbms",
    "computer networks",
)

KNOCKOUT_TERMS = (
    "must",
    "required",
    "only",
    "eligible",
    "availability",
    "onsite",
    "graduation year",
)


def parse_jd(text: str) -> ParsedJD:
    fields: dict[str, str] = {
        "company": UNKNOWN,
        "role_title": UNKNOWN,
        "location": UNKNOWN,
        "work_mode": UNKNOWN,
        "duration": UNKNOWN,
    }
    sections: dict[str, list[str]] = {
        "required_skills": [],
        "preferred_skills": [],
        "responsibilities": [],
        "eligibility": [],
    }
    active_section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        matched_field = _extract_field(line)
        if matched_field:
            key, value = matched_field
            fields[key] = value
            active_section = None
            continue

        section_name = _section_name(line)
        if section_name:
            active_section = section_name
            continue

        if active_section and _is_list_item(line):
            sections[active_section].append(_clean_list_item(line))

    combined_items = (
        sections["required_skills"]
        + sections["preferred_skills"]
        + sections["responsibilities"]
        + sections["eligibility"]
        + list(fields.values())
    )
    return ParsedJD(
        company=fields["company"],
        role_title=fields["role_title"],
        location=fields["location"],
        work_mode=fields["work_mode"],
        duration=fields["duration"],
        required_skills=sections["required_skills"],
        preferred_skills=sections["preferred_skills"],
        responsibilities=sections["responsibilities"],
        eligibility=sections["eligibility"],
        knockout_conditions=_find_knockout_conditions(sections["eligibility"]),
        exact_keywords=_find_exact_keywords(combined_items),
    )


def _extract_field(line: str) -> tuple[str, str] | None:
    for key, pattern in FIELD_PATTERNS.items():
        match = pattern.match(line)
        if match:
            return key, match.group("value").strip()
    return None


def _section_name(line: str) -> str | None:
    normalized = line.rstrip(":").strip().lower()
    return SECTION_ALIASES.get(normalized)


def _is_list_item(line: str) -> bool:
    return line.startswith(("-", "*", "•"))


def _clean_list_item(line: str) -> str:
    return line.lstrip("-*•").strip()


def _find_exact_keywords(items: list[str]) -> list[str]:
    haystack = "\n".join(items).lower()
    return [phrase for phrase in KEYWORD_PHRASES if phrase in haystack]


def _find_knockout_conditions(eligibility: list[str]) -> list[str]:
    conditions: list[str] = []
    for item in eligibility:
        lowered = item.lower()
        if any(term in lowered for term in KNOCKOUT_TERMS):
            conditions.append(item)
    return conditions
